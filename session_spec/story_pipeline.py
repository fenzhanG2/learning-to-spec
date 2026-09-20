import hashlib
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from .backend import CopilotBackend
from .locking import export_lock
from .ingest import resolve_session
from .pipeline import run_export, write_json
from .story_context import root_packet
from .story_article import reviewed_article, validate_article
from .story_draft import generate_draft
from .storage import file_hash
from .story_brief import validate_brief
from .story_editor import generate_edition, validate_edition, validate_feedback, validate_review
from .privacy import sanitize
from .story_insights import validate_insights
from .agent_handoff import render_agent, tool_ledger, validate_agent_detail
from .agent_package import EVIDENCE_RENDERER, HUMAN_PRESENTATION, LEGACY_PRESENTATION, PRESENTATION, render_agent_package, write_agent_package
from .language import resolve_language, validate_language
from .story_revision import load_revision
from .transfer_probe import effective_feedback
from .minimization_review import LEGACY_SCHEMA as LEGACY_MINIMIZATION_SCHEMA, SCHEMA as MINIMIZATION_SCHEMA, minimization_focus, validate_minimization
from .rationale_audit import SCHEMA as RATIONALE_SCHEMA, rationale_focus, validate_rationale_audit


RENDERER = Path(__file__).resolve().parent / "web/render-story.cjs"


def renderer_entry():
    bundle = RENDERER.with_name("render-story.bundle.cjs")
    if not bundle.is_file():
        return RENDERER
    manifest = json.loads(RENDERER.with_name("renderer-bundle.json").read_bytes())
    root = RENDERER.parents[2]
    sources = {"package-lock.json", "third_party/rendering-dependencies.txt", *(
        "session_spec/web/" + path.name for path in RENDERER.parent.glob("*.cjs") if path != bundle)}
    if (manifest.get("schema") != "renderer-bundle/v1" or set(manifest.get("sources", {})) != sources
            or set(manifest.get("outputs", {})) != {bundle.name, bundle.name + ".LEGAL.txt"}
            or any(file_hash(root / name) != expected for name, expected in manifest["sources"].items())
            or any(file_hash(RENDERER.parent / name) != expected for name, expected in manifest["outputs"].items())):
        raise ValueError("Renderer bundle is stale or changed; maintainers must run npm ci --ignore-scripts and npm run build")
    return bundle


def render_story(support, target):
    node = shutil.which("node")
    if not node:
        raise ValueError("Node.js is required for local HTML/SVG rendering")
    result = subprocess.run([node, str(renderer_entry()), str(support), str(target)], capture_output=True, text=True, encoding="utf-8", timeout=60)
    if result.returncode:
        raise ValueError("Story renderer failed; verify Node.js and the installed plugin. Source-only checkouts need npm ci --ignore-scripts and npm run build. " + result.stderr[-1200:])


def run_story(session, home, destination, from_export=None, model=None, gh_host=None, resume=False,
              max_calls=40, timeout=900, backend_factory=CopilotBackend, editorial_feedback=None, language="auto", revise_from=None):
    destination = Path(destination).expanduser().resolve()
    home = Path(home).resolve()
    if destination == home or destination.is_relative_to(home):
        raise ValueError("Story output must not be inside the Copilot source directory")
    source_path = Path(json.loads((Path(from_export) / "source.json").read_text(encoding="utf-8"))["source_path"]) if from_export else resolve_session(session, home)
    if destination == source_path.resolve().parent or destination.is_relative_to(source_path.resolve().parent):
        raise ValueError("Output cannot overwrite the historical session")
    with export_lock(destination):
        support = destination / "_support"
        existing = [path for path in destination.iterdir() if path.name != ".export.lock"]
        if existing and (not resume or not (support / "story-source.json").is_file()):
            raise ValueError("Existing output requires a matching --resume; use a new directory")
        support.mkdir(exist_ok=True)
        work = support / ".work"
        work.mkdir(exist_ok=True)
        backend = backend_factory(model=model, gh_host=gh_host, timeout=timeout, max_calls=max_calls)
        attempt = {"status": "running", "calls": []}
        prior_attempt = support / "story-attempt.json"
        if resume and prior_attempt.is_file():
            previous = json.loads(prior_attempt.read_bytes())
            attempt["previous_runs"] = [*previous.get("previous_runs", []),
                                        {key: value for key, value in previous.items() if key != "previous_runs"}]
        try:
            base = Path(from_export).resolve() if from_export else support / "canonical"
            if from_export and (destination == base or base.is_relative_to(destination)):
                raise ValueError("Cannot overwrite or contain the selected source export")
            if not from_export:
                result = run_export(session, home, base, model=model, gh_host=gh_host, language=language, resume=resume and (base / "source.json").is_file(),
                                    max_calls=max_calls, timeout=timeout, backend_factory=lambda **settings: backend)
                if result["status"] == "needs_review":
                    raise ValueError("Canonical facts still need review; refusing a success-looking human story")
            source = json.loads((base / "source.json").read_text(encoding="utf-8"))
            original = Path(source["source_path"])
            if destination == original.parent or destination.is_relative_to(original.parent):
                raise ValueError("Output cannot overwrite the historical session")
            with original.open("rb") as stream:
                snapshot = stream.read(source["snapshot_bytes"])
            if len(snapshot) != source["snapshot_bytes"] or hashlib.sha256(snapshot).hexdigest() != source["source_sha256"]:
                raise ValueError("Source snapshot hash mismatch")
            marker = support / "story-source.json"
            if marker.exists() and json.loads(marker.read_text(encoding="utf-8"))["source_sha256"] != source["source_sha256"]:
                raise ValueError("Cannot resume against a different source snapshot")
            write_json(marker, source)
            records = [json.loads(line) for line in (base / "evidence.jsonl").read_text(encoding="utf-8").splitlines() if line]
            packet = root_packet(records)
            if not packet:
                raise ValueError("Canonical export contains no root events. Select the canonical export, not a rendered story's private support directory; refusing story generation.")
            language_info = resolve_language(packet, language)
            output_language = language_info["language"]
            if len(json.dumps(packet, ensure_ascii=False)) > 800000:
                raise ValueError("Full-evidence story currently supports up to 800,000 characters; no silent truncation performed")
            canonical_path = base / "spec.json"
            canonical = json.loads(canonical_path.read_text(encoding="utf-8")).get("spec") if canonical_path.is_file() else None
            write_json(work / "input.json", packet)
            write_json(work / "source.json", source)
            (work / "evidence.jsonl").write_text("\n".join(json.dumps(event, ensure_ascii=False) for event in packet) + "\n", encoding="utf-8")
            feedback_path = Path(editorial_feedback) if editorial_feedback else work / "editorial-feedback.json"
            feedback_document = json.loads(feedback_path.read_text(encoding="utf-8")) if feedback_path.is_file() else {"source_sha256": source["source_sha256"], "issues": []}
            if editorial_feedback and not feedback_path.is_file():
                raise ValueError("Editorial feedback file does not exist")
            feedback = sanitize(validate_feedback(feedback_document, packet, source["source_sha256"]))
            write_json(work / "editorial-feedback.json", {"source_sha256": source["source_sha256"], "issues": feedback})
            if revise_from:
                draft, origin = load_revision(revise_from, packet, source)
                print("[story] revising source-matched prior content under the current contract", flush=True)
            else:
                print("[story] generating one coherent human and Agent draft", flush=True)
                draft = generate_draft(work, packet, canonical, backend, model, language)
                origin = {"method": "joint_generation", "source_sha256": source["source_sha256"]}
            write_json(work / "draft-origin.json", origin)
            def detailed_validator(article, events):
                errors = validate_article(article, events)
                if isinstance(article, dict) and (not isinstance(article.get("agent_detail"), dict) or article["agent_detail"].get("schema") != "agent-detail/v3"):
                    errors.append("New Agent handoff must use agent-detail/v3 with resume, continuation, recipes and tool_steps.usage summaries")
                return errors
            edition = generate_edition(work, draft, packet, backend, detailed_validator, model=model,
                                       feedback=feedback, max_repairs=4, language=language, max_structural_repairs=4, transfer_probe=True)
            article, insights = edition["article"], edition["insights"]
            staged = work / "edition-render"
            staged.mkdir(exist_ok=True)
            for name in ("article", "insights", "brief"):
                write_json(staged / (name + ".json"), edition[name])
                write_json(staged / (name + "-receipt.json"), {"status": "completed", "stage": "whole-document-edition",
                           "output_sha256": file_hash(staged / (name + ".json")), "edition_sha256": file_hash(work / "edition.json")})
            agent_files = write_agent_package(article, packet, output_language, staged, staged)
            write_json(staged / "language.json", language_info)
            write_json(staged / "human-presentation.json", HUMAN_PRESENTATION)
            write_json(staged / "tool-ledger.json", tool_ledger(packet))
            temporary_html = work / "human-spec.pending.html"
            render_story(staged, temporary_html)
            temporary_agent = work / "agent-spec.pending.md"
            temporary_agent.write_text(agent_files["agent-spec.md"], encoding="utf-8")
            for filename in ("article.json", "article-receipt.json", "insights.json", "insights-receipt.json", "brief.json", "brief-receipt.json", "agent-rendered.md", "evidence-rendered.md", "agent-presentation.json", "human-presentation.json", "language.json", "tool-ledger.json"):
                shutil.copy2(staged / filename, support / filename)
            for filename in ("edition.json", "edition-receipt.json", "editorial-feedback.json", "input.json", "source.json", "evidence.jsonl", "draft-origin.json"):
                shutil.copy2(work / filename, support / filename)
            attempt.update(status="reviewed_draft", output_schema="story-output/v9", generation_method="markdown-files-handoff/v1", evidence_renderer=EVIDENCE_RENDERER, language=output_language, source_sha256=source["source_sha256"],
                           architecture=insights["architecture"]["decision"], human_inputs=sum(bool(event.get("human_input")) for event in packet),
                           hashes={"human-spec.html": file_hash(temporary_html), **{name: file_hash(staged / name) for name in agent_files}},
                           support_hashes={name: file_hash(support / name) for name in ("article.json", "insights.json", "brief.json", "edition.json", "edition-receipt.json", "editorial-feedback.json", "article-receipt.json", "insights-receipt.json", "brief-receipt.json", "input.json", "source.json", "evidence.jsonl", "agent-rendered.md", "evidence-rendered.md", "agent-presentation.json", "human-presentation.json", "language.json", "tool-ledger.json", "draft-origin.json")},
                           limitations=["Automatic review is not proof of correctness; source-project tests were not rerun."])
            temporary_agent.replace(destination / "agent-spec.md")
            shutil.copy2(staged / "evidence.md", destination / "evidence.md")
            temporary_html.replace(destination / "human-spec.html")
            attempt["calls"] = backend.calls
            write_json(support / "story-report.json", attempt)
            return {"status": attempt["status"], "output": str(destination), "human_spec": str(destination / "human-spec.html"),
                    "agent_spec": str(destination / "agent-spec.md"), "evidence": str(destination / "evidence.md"), "architecture": attempt["architecture"],
                    "human_inputs": attempt["human_inputs"], "model_calls": len(backend.calls)}
        except Exception as error:
            attempt.update(status="failed", error=str(error))
            raise
        finally:
            attempt["calls"] = backend.calls
            write_json(support / "story-attempt.json", attempt)


def validate_story(directory):
    directory = Path(directory).resolve()
    support = directory / "_support"
    report = json.loads((support / "story-report.json").read_text(encoding="utf-8"))
    article = json.loads((support / "article.json").read_text(encoding="utf-8"))
    insights = json.loads((support / "insights.json").read_text(encoding="utf-8"))
    packet = json.loads((support / "input.json").read_text(encoding="utf-8"))
    errors = validate_article(article, packet) + validate_insights(insights, article, packet)
    brief_path = support / "brief.json"
    if brief_path.is_file():
        errors.extend(validate_brief(json.loads(brief_path.read_bytes()), packet))
        if "brief.json" not in report["support_hashes"]:
            errors.append("Opening brief is not bound to the published report")
    elif report.get("output_schema") in ("story-output/v2", "story-output/v3", "story-output/v4", "story-output/v5", "story-output/v6", "story-output/v7", "story-output/v8", "story-output/v9") or "brief.json" in report["support_hashes"]:
        errors.append("Published opening brief is missing")
    if report.get("output_schema") in ("story-output/v3", "story-output/v4", "story-output/v5", "story-output/v6", "story-output/v7", "story-output/v8", "story-output/v9"):
        edition_path, receipt_path = support / "edition.json", support / "edition-receipt.json"
        edition = {}
        if not edition_path.is_file() or not receipt_path.is_file():
            errors.append("Whole-document edition or review receipt is missing")
        else:
            edition = json.loads(edition_path.read_bytes())
            receipt = json.loads(receipt_path.read_bytes())
            errors.extend(validate_edition(edition, packet, validate_article))
            feedback_path = support / "editorial-feedback.json"
            feedback = validate_feedback(json.loads(feedback_path.read_bytes()), packet, report["source_sha256"]) if feedback_path.is_file() else []
            try:
                feedback = effective_feedback(receipt, feedback, packet)
            except ValueError as error:
                errors.append(str(error))
            errors.extend(validate_review(receipt.get("review"), feedback, protocol=receipt.get("review_protocol")))
            if receipt.get("review_protocol") == "grounded-findings/v4" and isinstance(receipt.get("review"), dict):
                minimization_schema = receipt.get("identity", {}).get("minimization_focus")
                if minimization_schema not in (LEGACY_MINIMIZATION_SCHEMA, MINIMIZATION_SCHEMA):
                    errors.append("Unknown or missing minimization index identity")
                else:
                    errors.extend(validate_minimization(receipt["review"], edition, packet, minimization_focus(edition, packet, minimization_schema)))
            rationale_schema = receipt.get("identity", {}).get("rationale_audit")
            if rationale_schema is not None:
                if rationale_schema != RATIONALE_SCHEMA or not isinstance(receipt.get("review"), dict):
                    errors.append("Unknown rationale audit identity or invalid review")
                else:
                    errors.extend(validate_rationale_audit(receipt["review"], rationale_focus(edition, packet), packet))
            if receipt.get("status") != "completed" or receipt.get("review", {}).get("issues") or receipt.get("output_sha256") != file_hash(edition_path):
                errors.append("Whole-document review does not match the accepted edition")
            if not brief_path.is_file() or edition != {"article": article, "insights": insights, "brief": json.loads(brief_path.read_bytes())}:
                errors.append("Published components differ from the reviewed edition")
    for root, hashes in ((directory, report["hashes"]), (support, report["support_hashes"])):
        for filename, expected in hashes.items():
            if Path(filename).name != filename or not (root / filename).is_file() or file_hash(root / filename) != expected:
                errors.append("Published content differs: " + filename)
    trajectory_style = {"story-output/v9": "handoff-split", "story-output/v8": "handoff-split", "story-output/v7": "handoff-legacy", "story-output/v6": "decisions"}.get(report.get("output_schema"), "expanded")
    if report.get("output_schema") == "story-output/v7":
        if report.get("evidence_renderer") == "roles/v2":
            trajectory_style = "handoff"
        elif report.get("evidence_renderer") is not None:
            errors.append("Unknown evidence renderer policy")
    if report.get("output_schema") in {"story-output/v8", "story-output/v9"}:
        markdown_files = report["output_schema"] == "story-output/v9"
        if report.get("evidence_renderer") not in ({"companion/v2", "companion/v3", "companion/v4", "companion/v5", "companion/v6", EVIDENCE_RENDERER} if markdown_files else {"companion/v1", "companion/v2"}):
            errors.append("Unknown companion evidence renderer policy")
        if markdown_files and report.get("evidence_renderer") == "companion/v3":
            trajectory_style = "handoff-portable"
        if markdown_files and report.get("evidence_renderer") == "companion/v4":
            trajectory_style = "handoff-portable-v2"
        if markdown_files and report.get("evidence_renderer") == "companion/v5":
            trajectory_style = "handoff-portable-v3"
        if markdown_files and report.get("evidence_renderer") == "companion/v6":
            trajectory_style = "handoff-portable-v4"
        if markdown_files and report.get("evidence_renderer") == EVIDENCE_RENDERER:
            trajectory_style = "handoff-portable-v5"
        if markdown_files or report.get("evidence_renderer") == "companion/v2":
            policy = support / "agent-presentation.json"
            expected_policy = PRESENTATION if markdown_files else LEGACY_PRESENTATION
            if "agent-presentation.json" not in report["support_hashes"] or not policy.is_file() or json.loads(policy.read_bytes()) != expected_policy:
                errors.append("Agent presentation policy is missing or invalid")
        for filename, root, hashes in (("evidence.md", directory, report["hashes"]), ("evidence-rendered.md", support, report["support_hashes"])):
            if filename not in hashes or not (root / filename).is_file():
                errors.append("Required evidence companion is missing or not hash-bound: " + filename)
            elif (root / filename).read_text(encoding="utf-8") != render_agent_package(article, packet, report["language"], trajectory_style)["evidence.md"]:
                errors.append("Evidence companion differs from the reviewed handoff: " + filename)
    human_policy = support / "human-presentation.json"
    if human_policy.is_file():
        if "human-presentation.json" not in report["support_hashes"] or json.loads(human_policy.read_bytes()) != HUMAN_PRESENTATION:
            errors.append("Human presentation policy is missing or invalid")
    elif report.get("evidence_renderer") in {"companion/v4", "companion/v5", "companion/v6", EVIDENCE_RENDERER}:
        errors.append("Human presentation policy is missing or invalid")
    if report.get("output_schema") in ("story-output/v4", "story-output/v5", "story-output/v6", "story-output/v7", "story-output/v8", "story-output/v9"):
        errors.extend(validate_agent_detail(article.get("agent_detail"), packet))
        if report["output_schema"] in ("story-output/v5", "story-output/v6") and article.get("agent_detail", {}).get("schema") != "agent-detail/v2":
            errors.append("Published actionable handoff requires agent-detail/v2")
        if report["output_schema"] in {"story-output/v7", "story-output/v8", "story-output/v9"} and article.get("agent_detail", {}).get("schema") != "agent-detail/v3":
            errors.append("Published grounded handoff requires agent-detail/v3")
        language_info = json.loads((support / "language.json").read_bytes())
        errors.extend(validate_language(edition, language_info.get("requested", report["language"]), packet))
        if json.loads((support / "tool-ledger.json").read_bytes()) != tool_ledger(packet):
            errors.append("Tool ledger differs from the complete observable source")
        if language_info.get("language") != report["language"]:
            errors.append("Output language differs from its publication receipt")
        if (support / "agent-rendered.md").read_text(encoding="utf-8") != render_agent(article, packet, report["language"], trajectory_style):
            errors.append("Rendered Agent source differs from the reviewed detailed handoff")
    if (directory / "agent-spec.md").read_text(encoding="utf-8") != render_agent(article, packet, report.get("language", "zh-CN"), trajectory_style):
        errors.append("Agent document differs from accepted article")
    with tempfile.TemporaryDirectory(prefix="story-verify-") as temporary:
        rendered = Path(temporary) / "human-spec.html"
        render_story(support, rendered)
        if rendered.read_bytes() != (directory / "human-spec.html").read_bytes():
            errors.append("HTML differs from accepted article/insights/brief")
    return {"valid": not errors, "issues": errors, "status": report["status"], "note": "Structural consistency, not proof of factual correctness."}
