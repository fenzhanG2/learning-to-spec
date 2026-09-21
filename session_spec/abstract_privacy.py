import copy
import json
import tempfile
from pathlib import Path

from .agent_package import HUMAN_PRESENTATION, PRESENTATION
from .backend import check_preparation_deadline
from .delivery import deliver, filenames, preferences
from .ingest import read_session, resolve_session
from .privacy import content_redaction
from .reduction import apply_review, digest, load_review, prepare_full_session, review_identity, scan_session, transform
from .reduction_semantic import semantic_review
from .storage import file_hash, unlinked_path, write_json as store_json


PIPELINE = "abstract-then-redact/v1"


def plain_path(path):
    return unlinked_path(path)


def preflight_tree(path):
    pending = [plain_path(path)]
    while pending:
        current = plain_path(pending.pop())
        if current.is_dir():
            pending.extend(current.iterdir())


def write_json(path, value):
    path = plain_path(path)
    plain_path(path.with_suffix(path.suffix + ".tmp"))
    store_json(path, value)


def abstract_surface(story, selection):
    story = plain_path(story)
    surface = {}
    if selection["readers"] in {"human", "both"}:
        edition = json.loads(plain_path(story / "_support/edition.json").read_bytes())
        article = copy.deepcopy(edition["article"])
        article.pop("agent_detail", None)
        article["agent_markdown"] = ""
        surface["human"] = {"article": article, "brief": edition["brief"], "insights": edition["insights"]}
    if selection["readers"] in {"agent", "both"}:
        for key, name in (("agent", "agent-spec.md"), ("evidence", "evidence.md")):
            surface[key] = plain_path(story / name).read_bytes().decode("utf-8")
    return [{"type": "artifact.abstracted", "data": surface}]


def prepare_abstract_review(source, home, review_directory, audience, selection, privacy_mode, custom=None,
                            purpose="Technical story and actionable Agent handoff", fast=False, **settings):
    from .story_pipeline import run_story, validate_story

    check_preparation_deadline()
    if privacy_mode not in {"full", "llm"}:
        raise ValueError("Choose no redaction or rules plus Copilot review")
    selection = preferences(selection.get("readers"), selection.get("destination"), audience)
    source = plain_path(resolve_session(str(source), Path(home)))
    review_directory = plain_path(review_directory)
    directory = plain_path(review_directory.parent / "abstraction")
    if (not review_directory.name or review_directory.name == "abstraction" or directory == source.parent or directory.is_relative_to(source.parent)
            or directory.is_relative_to(Path(home).resolve())):
        raise ValueError("Abstraction requires a private workspace outside the source session")
    if directory.exists() or review_directory.exists():
        raise ValueError("Use a fresh abstraction workspace; completed reviews can be resumed without regeneration")
    canonical = directory / "canonical"
    canonical.mkdir(parents=True, mode=0o700)
    backend = None
    with content_redaction(False):
        metadata, records = read_session(source, Path(home))
        write_json(canonical / "source.json", metadata)
        (canonical / "evidence.jsonl").write_text("".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records), encoding="utf-8", newline="\n")
        if fast:
            from .backend import CopilotBackend
            from .fast_story import run_fast_story, validate_fast_story

            factory = settings.get("backend_factory", CopilotBackend)
            requested_model = settings.get("model")
            backend = factory(model=requested_model, auto_tier=None,
                              gh_host=settings.get("gh_host"), timeout=120, max_calls=3,
                              reasoning_effort=None)
            run_fast_story(canonical, directory / "story", backend, language=settings.get("language", "auto"), model=settings.get("model"))
            validation = validate_fast_story(directory / "story")
        else:
            run_story(None, home, directory / "story", from_export=canonical, **settings)
            validation = validate_story(directory / "story")
    check_preparation_deadline()
    if not validation["valid"]:
        raise ValueError("The private abstraction failed validation; no privacy review or delivery was started")
    surface = abstract_surface(directory / "story", selection)
    artifact = directory / "source/events.jsonl"
    artifact.parent.mkdir(mode=0o700)
    artifact.write_text("".join(json.dumps(event, ensure_ascii=False) + "\n" for event in surface), encoding="utf-8", newline="\n")
    manifest = {"schema": PIPELINE, "source_path": str(source), "source_sha256": metadata["source_sha256"],
                "artifact_sha256": file_hash(artifact), "privacy_mode": privacy_mode, "preferences": selection,
                "audience": audience, "story_report_sha256": file_hash(directory / "story/_support/story-report.json"),
                "language": json.loads((directory / "story/_support/language.json").read_bytes())["language"],
                "note": "Private abstraction precedes privacy review. Only selected reader surfaces, including Agent evidence, are reviewed for delivery."}
    if fast:
        manifest["quality_profile"] = "bounded-source-review/v1"
    if file_hash(source) != manifest["source_sha256"]:
        raise ValueError("The session changed during abstraction; use a new snapshot")
    write_json(directory / "abstraction.json", manifest)
    if privacy_mode == "full":
        review = prepare_full_session(artifact, home, review_directory, audience, selection)
    else:
        review = scan_session(artifact, home, review_directory, audience=audience, preferences=selection, privacy_mode="llm", custom=custom,
                              purpose="Review already abstracted reader documents and selected evidence; preserve technical meaning. Purpose: " + purpose)
    review.update(pipeline=PIPELINE, abstraction_sha256=file_hash(directory / "abstraction.json"))
    review["review_id"] = review_identity(review)
    write_json(review_directory / "review.json", review)
    if fast:
        from .fast_quality import DraftPrivacyBackend, FastQualityBackend, review_source_quality
        from .reduction_semantic import PrivacyReviewFailure

        packet = json.loads((directory / "story/_support/input.json").read_bytes())
        quality_path = directory / "story/_support/fast-quality.json"
        reviewer = FastQualityBackend(backend, packet, artifact, directory / "story/_support/story-report.json", quality_path)
        write_json(directory / "story/_support/source-review-started.json", {"schema": "isolated-source-review/v1"})
        review_source_quality(reviewer, surface)
        if privacy_mode == "llm":
            remaining = max(0, 3 - len(backend.calls))
            if not remaining:
                raise PrivacyReviewFailure("Draft repairs used the model-call budget before the isolated privacy review; no privacy approval was recorded")
            review = semantic_review(review_directory, consent=True, backend=DraftPrivacyBackend(backend),
                                     max_calls=remaining, repair_attempts=min(1, remaining - 1))
        check_preparation_deadline()
        manifest["quality_review_sha256"] = file_hash(quality_path)
        manifest["review_isolation"] = "source-quality-and-draft-privacy/v1"
        write_json(directory / "abstraction.json", manifest)
        review["abstraction_sha256"] = file_hash(directory / "abstraction.json")
        review["review_id"] = review_identity(review)
        write_json(review_directory / "review.json", review)
        return review
    if privacy_mode == "llm":
        semantic_settings = {key: value for key, value in settings.items() if key in {"model", "gh_host", "max_calls"}}
        review = semantic_review(review_directory, consent=True, **semantic_settings)
    return review


def load_abstract_review(directory, review=None, *, validate_parent=False):
    directory = plain_path(directory)
    review = load_review(directory)[0] if review is None else review
    abstraction = plain_path(directory.parent / "abstraction")
    preflight_tree(directory)
    preflight_tree(abstraction)
    manifest_path = plain_path(abstraction / "abstraction.json")
    if review.get("pipeline") != PIPELINE or file_hash(manifest_path) != review.get("abstraction_sha256"):
        raise ValueError("Abstract-first privacy provenance changed or is missing")
    manifest = json.loads(manifest_path.read_bytes())
    artifact = plain_path(abstraction / "source/events.jsonl")
    if (manifest.get("schema") != PIPELINE or plain_path(review["source_path"]) != artifact
            or file_hash(artifact) != manifest.get("artifact_sha256") or review.get("source_sha256") != manifest["artifact_sha256"]
            or review.get("preferences") != manifest.get("preferences") or review.get("privacy_mode") != manifest.get("privacy_mode")
            or review.get("audience") != manifest.get("audience")
            or file_hash(plain_path(manifest["source_path"])) != manifest.get("source_sha256")):
        raise ValueError("The abstraction, source snapshot or approved privacy settings changed")
    story = plain_path(abstraction / "story")
    if (story / "_support/privacy-transform.json").exists():
        raise ValueError("A private source abstraction cannot itself be a privacy-transformed delivery")
    if file_hash(plain_path(story / "_support/story-report.json")) != manifest["story_report_sha256"]:
        raise ValueError("Private abstraction validation receipt changed")
    original = [json.loads(line) for line in artifact.read_text(encoding="utf-8").splitlines()]
    if original != abstract_surface(story, manifest["preferences"]):
        raise ValueError("Reviewed surfaces differ from the private abstracted documents")
    profile = manifest.get("quality_profile")
    if profile not in {None, "bounded-source-review/v1"}:
        raise ValueError("Unknown abstraction quality profile")
    if profile:
        from .fast_quality import validate_quality_receipt

        validate_quality_receipt(abstraction, manifest)
    if validate_parent:
        with content_redaction(False):
            if profile:
                from .fast_story import validate_fast_story

                validation = validate_fast_story(story)
            else:
                from .story_pipeline import validate_story

                validation = validate_story(story)
        if not validation["valid"]:
            raise ValueError("The private abstraction is no longer valid")
    return manifest


def render_surface(events, manifest, directory):
    from .story_pipeline import render_story

    names = filenames(manifest["preferences"]["readers"])
    expected = ({"human"} if "human-spec.html" in names else set()) | ({"agent", "evidence"} if "agent-spec.md" in names else set())
    if (not isinstance(events, list) or len(events) != 1 or events[0].get("type") != "artifact.abstracted"
            or not isinstance(events[0].get("data"), dict) or set(events[0]["data"]) != expected):
        raise ValueError("The selected abstract document surface changed shape")
    surface = events[0]["data"]
    directory = plain_path(directory)
    preflight_tree(directory)
    directory.mkdir(parents=True, exist_ok=True)
    support = directory / "_support"
    support.mkdir(exist_ok=True)
    for key, name in (("agent", "agent-spec.md"), ("evidence", "evidence.md")):
        if key in surface:
            if not isinstance(surface[key], str):
                raise ValueError("Abstract Markdown must remain text")
            plain_path(directory / name).write_bytes(surface[key].encode("utf-8"))
    if "human" in surface:
        human = surface["human"]
        if not isinstance(human, dict) or set(human) != {"article", "brief", "insights"}:
            raise ValueError("Abstract Human document changed shape")
        for name, value in human.items():
            write_json(support / (name + ".json"), value)
        write_json(support / "agent-presentation.json", PRESENTATION)
        write_json(support / "human-presentation.json", HUMAN_PRESENTATION)
        write_json(support / "language.json", {"language": manifest["language"]})
        if "evidence" in surface:
            plain_path(support / "evidence-rendered.md").write_bytes(surface["evidence"].encode("utf-8"))
        with content_redaction(False):
            render_story(support, plain_path(directory / "human-spec.html"))
    return {name: file_hash(directory / name) for name in names}


def generate_abstract_private(directory, decisions, output, home, resume=False, confirm_choices=False, **settings):
    check_preparation_deadline()
    if confirm_choices is not True:
        raise ValueError("Explicit approval is required before applying privacy choices and exporting")
    directory, output = plain_path(directory), plain_path(output)
    review, baseline = load_review(directory)
    manifest = load_abstract_review(directory, review, validate_parent=True)
    if output.parent != directory.parent or output == directory or output.name == "abstraction":
        raise ValueError("Final export must be a separate job-local generation directory")
    preflight_tree(output)
    if output.exists() and any(output.iterdir()):
        if not resume:
            raise ValueError("Use a new generation directory or resume the identical choices")
        receipt = json.loads((output / "reduced/reduction.json").read_bytes())
        if receipt.get("review_id") != review["review_id"] or receipt.get("decisions_sha256") != digest(decisions):
            raise ValueError("Privacy choices changed; use a new generation directory")
    else:
        apply_review(directory, decisions, output / "reduced")
    approval = output / "approved-decisions.json"
    if approval.exists():
        if json.loads(approval.read_bytes()) != decisions:
            raise ValueError("This generation's immutable approval differs; use a new generation")
    else:
        write_json(approval, decisions)
    events, operations = transform(review, baseline, decisions)
    reduced = plain_path(output / "reduced/events.jsonl")
    if events != [json.loads(line) for line in reduced.read_text(encoding="utf-8").splitlines()]:
        raise ValueError("Reduced documents changed; previous approval cannot be reused")
    story = output / "story"
    hashes = render_surface(events, manifest, story)
    source = {"source_path": str(reduced), "source_sha256": file_hash(reduced)}
    write_json(story / "_support/source.json", source)
    write_json(story / "_support/privacy-transform.json", {"schema": PIPELINE, "review_id": review["review_id"],
               "review_directory": directory.name,
               "approval_sha256": file_hash(approval),
               "abstraction_sha256": review["abstraction_sha256"], "decisions_sha256": digest(decisions),
               "reduced_sha256": source["source_sha256"], "files": hashes, "operations": operations,
               "note": "The private abstraction was reviewed before redaction. These final files are deterministic applications of approved changes, not newly generated or separately model-certified prose."})
    validation = validate_abstract_story(story)
    check_preparation_deadline()
    if not validation["valid"]:
        raise ValueError("Final redacted output failed its provenance or deterministic-render checks")
    result = {"status": "privacy_transformed", "pipeline": PIPELINE,
              "privacy": "Abstracted first; approved disclosure changes applied to selected reader documents and evidence. No model calls after privacy approval.",
              "model_calls_after_approval": 0, **deliver(output, manifest["preferences"])}
    for key, name in (("human_spec", "human-spec.html"), ("agent_spec", "agent-spec.md"), ("evidence", "evidence.md")):
        if name in result["files"]:
            result[key] = str(output / "deliverables" / name)
    check_preparation_deadline()
    return result


def validate_abstract_story(directory):
    directory = plain_path(directory)
    errors = []
    try:
        if directory.name != "story":
            raise ValueError("Unknown final abstracted-output layout")
        output = directory.parent
        receipt = json.loads(plain_path(directory / "_support/privacy-transform.json").read_bytes())
        review_name = receipt.get("review_directory")
        if not isinstance(review_name, str) or not review_name or review_name in {".", "..", "abstraction"} or Path(review_name).name != review_name:
            raise ValueError("Invalid job-local privacy-review directory")
        review_directory = plain_path(output.parent / review_name)
        review, baseline = load_review(review_directory)
        manifest = load_abstract_review(review_directory, review, validate_parent=True)
        approval = plain_path(output / "approved-decisions.json")
        decisions = json.loads(approval.read_bytes())
        if (receipt.get("schema") != PIPELINE or receipt.get("review_id") != review["review_id"]
                or receipt.get("abstraction_sha256") != review["abstraction_sha256"]
                or receipt.get("approval_sha256") != file_hash(approval)
                or receipt.get("decisions_sha256") != digest(decisions)):
            raise ValueError("Final files do not match their approved abstraction and privacy choices")
        events, operations = transform(review, baseline, decisions)
        reduced = plain_path(output / "reduced/events.jsonl")
        reduction = json.loads(plain_path(output / "reduced/reduction.json").read_bytes())
        source = json.loads(plain_path(directory / "_support/source.json").read_bytes())
        if (receipt.get("operations") != operations or file_hash(reduced) != receipt.get("reduced_sha256")
                or events != [json.loads(line) for line in reduced.read_text(encoding="utf-8").splitlines()]
                or source != {"source_path": str(reduced), "source_sha256": file_hash(reduced)}
                or reduction.get("review_id") != review["review_id"] or reduction.get("decisions_sha256") != digest(decisions)
                or reduction.get("reduced_sha256") != file_hash(reduced) or reduction.get("preferences") != manifest["preferences"]
                or reduction.get("audience") != manifest["audience"]):
            raise ValueError("Final privacy provenance changed")
        with tempfile.TemporaryDirectory(prefix="abstract-render-check-") as temporary:
            hashes = render_surface(events, manifest, Path(temporary))
        if receipt.get("files") != hashes:
            raise ValueError("Final files differ from the approved transformation")
        for name, expected in hashes.items():
            if file_hash(plain_path(directory / name)) != expected:
                raise ValueError("A final reader file changed after privacy review")
    except (ValueError, OSError, KeyError, TypeError, AttributeError, IndexError) as error:
        errors.append(str(error))
    return {"valid": not errors, "issues": errors, "status": "privacy_transformed",
            "note": "Verified source abstraction and deterministic approved redaction; not a new claim of semantic correctness or anonymity."}
