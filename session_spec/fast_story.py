import copy
import json
import re
import tempfile
from pathlib import Path

from .agent_handoff import tool_ledger
from .agent_package import EVIDENCE_RENDERER, HUMAN_PRESENTATION, PRESENTATION, render_agent_package, write_agent_package
from .backend import ModelResponseError, PreparationTimeoutError
from .language import resolve_language, validate_language
from .locking import export_lock
from .privacy import content_redaction
from .storage import PROMPTS, digest, file_hash, unlinked_path, write_json
from .story_article import validate_article
from .story_context import reference_role_guidance, root_packet
from .story_editor import validate_edition
from .story_pipeline import render_story


SCHEMA = "fast-story/v1"
PROFILE = "production-joint-draft/v1"
STATUS = "structurally_valid_unreviewed"
MAX_INPUT_CHARACTERS = 800000
MAX_EDITION_CHARACTERS = 24000
MAX_REPAIR_CHARACTERS = 6000
MAX_REPLACEMENTS = 8
OUTPUT_FILES = {"human-spec.html", "agent-spec.md", "evidence.md"}
SUPPORT_FILES = {"source.json", "evidence.jsonl", "input.json", "edition.json", "article.json", "brief.json", "insights.json",
                 "language.json", "agent-presentation.json", "human-presentation.json", "agent-rendered.md", "evidence-rendered.md",
                 "tool-ledger.json", "fast-contract.md", "fast-receipt.json"}
DRAFT_FAILURE_CODES = {"draft_references_invalid", "draft_structure_invalid"}


class DraftValidationError(ValueError):
    def __init__(self, message, issues):
        super().__init__(message)
        self.error_code = ("draft_references_invalid" if any("source refs" in issue for issue in issues)
                           else "draft_structure_invalid")


def plain_path(value):
    return unlinked_path(value)


def source_snapshot(source):
    path = plain_path(source["source_path"])
    size = source.get("snapshot_bytes")
    if type(size) is not int or not 0 < size <= 32 * 1024 * 1024:
        raise ValueError("Invalid bounded source snapshot size")
    with path.open("rb") as stream:
        content = stream.read(size)
    if len(content) != size or digest(content) != source.get("source_sha256"):
        raise ValueError("Source snapshot hash mismatch")
    return path


def source_packet(records):
    if not isinstance(records, list) or not records:
        raise ValueError("Fast story requires complete canonical source records")
    refs = [event.get("ref") if isinstance(event, dict) else None for event in records]
    if (any(not isinstance(ref, str) or not re.fullmatch(r"E\d{6}", ref) for ref in refs)
            or len(set(refs)) != len(refs)
            or any(not isinstance(event.get("type"), str) or event.get("origin") not in {"root", "delegated", "injected"} for event in records)):
        raise ValueError("Invalid canonical event identity, role or type")
    with content_redaction(False):
        roots = {event["ref"]: event for event in root_packet(records)}
    packet = copy.deepcopy(records)
    for event in packet:
        event.pop("human_input", None)
        root = roots.get(event["ref"])
        if root and root.get("human_input"):
            event["human_input"] = root["human_input"]
    if not any(event.get("human_input") for event in packet):
        raise ValueError("Fast story requires an observable root human input")
    return packet


def handoff_attention(packet):
    from .story_grounding import text_values

    patterns = (
        ("reported_security_warning", r"\b(?:hard[- ]?coded|remov\w*[/ ]rotat\w*|rotate[ds]?|revok\w*)\b|密钥泄露|轮换密钥"),
        ("acceptance_or_failure_condition", r"\bskip(?:ping|ped)?\b|\bos\.Exit\(\s*0\s*\)|\b(?:false[- ]green|no[- ]op|rejected)\b|跳过测试|拒绝修改"),
    )
    candidates = []
    for event in packet:
        for category, pattern in patterns:
            for text in text_values(event):
                match = re.search(pattern, text, re.I)
                if match:
                    start = max(0, match.start() - 220)
                    excerpt = text[start:min(len(text), match.end() + 360)]
                    candidates.append({"ref": event["ref"], "type": event["type"], "category": category, "quote": excerpt})
                    break
    return {"candidates": candidates[:12], "additional_candidates": max(0, len(candidates) - 12),
            "scope": "Literal navigation hints, not verified risks or extra user requirements. Complete source remains supplied."}


def structural_issues(edition, packet, language):
    if len(json.dumps(edition, ensure_ascii=False)) > MAX_EDITION_CHARACTERS:
        return ["Compact draft exceeds 24,000 JSON characters; shorten prose without dropping requirements or source coverage"]
    if (isinstance(edition, dict) and isinstance(edition.get("article"), dict)
            and {"brief", "insights"} & edition["article"].keys()):
        return ["Ambiguous or invalid root envelope: brief and insights belong only beside article, never inside it; preserve source-supported content without choosing or discarding conflicting copies automatically"]
    try:
        issues = validate_edition(edition, packet, validate_article)
        if isinstance(edition, dict) and isinstance(edition.get("article"), dict):
            if edition["article"].get("agent_detail", {}).get("schema") != "agent-detail/v3":
                issues.append("Fast production handoff requires agent-detail/v3")
        if not issues:
            issues.extend(validate_language(edition, language, packet))
        return issues
    except (KeyError, TypeError, AttributeError, ValueError):
        return ["Malformed edition fields; return the exact joint article/brief/insights schema"]


def normalize_edition_envelope(response):
    candidate = copy.deepcopy(response)
    if (not isinstance(candidate, dict) or not set(candidate) <= {"article", "brief", "insights"}
            or not isinstance(candidate.get("article"), dict)):
        return candidate, []
    article = candidate["article"]
    schemas = {"brief": "story-brief/v1", "insights": "story-insights/v1"}
    if any(name in candidate and name in article for name in schemas):
        return candidate, []
    moves = []
    for name, schema in schemas.items():
        if name not in candidate and isinstance(article.get(name), dict) and article[name].get("schema") == schema:
            candidate[name] = article.pop(name)
            moves.append({"from": ["article", name], "to": [name]})
    return candidate, moves


def apply_replacements(candidate, response):
    if not isinstance(response, dict) or set(response) != {"replacements"}:
        raise ValueError("Repair requires only a replacements array, not a regenerated draft")
    if len(json.dumps(response, ensure_ascii=False, allow_nan=False)) > MAX_REPAIR_CHARACTERS:
        raise ValueError("Repair exceeds 6,000 JSON characters")
    replacements = response["replacements"]
    if not isinstance(replacements, list) or not 1 <= len(replacements) <= MAX_REPLACEMENTS:
        raise ValueError("Repair requires 1–8 exact field replacements")
    result = copy.deepcopy(candidate)
    paths = []
    for replacement in replacements:
        if not isinstance(replacement, dict) or set(replacement) != {"path", "value"}:
            raise ValueError("Each replacement requires exactly path and value")
        path = replacement["path"]
        if (not isinstance(path, list) or not 2 <= len(path) <= 16
                or any(type(part) not in (str, int) for part in path)
                or path[0] not in ("article", "brief", "insights")):
            raise ValueError("Replacement must target an existing field beneath article, brief or insights")
        if any(path[:len(previous)] == previous or previous[:len(path)] == path for previous in paths):
            raise ValueError("Duplicate or overlapping replacement paths are forbidden")
        paths.append(path)
        parent = result
        for index, part in enumerate(path):
            if isinstance(parent, dict):
                valid = type(part) is str and part in parent
            elif isinstance(parent, list):
                valid = type(part) is int and 0 <= part < len(parent)
            else:
                valid = False
            if not valid:
                raise ValueError("Replacement path does not identify an existing field or zero-based array item")
            if index == len(path) - 1:
                parent[part] = copy.deepcopy(replacement["value"])
            else:
                parent = parent[part]
    return result


def run_fast_story(from_export, destination, backend, *, language="auto", model=None):
    base, destination = plain_path(from_export), plain_path(destination)
    source_bytes = plain_path(base / "source.json").read_bytes()
    evidence_bytes = plain_path(base / "evidence.jsonl").read_bytes()
    source = json.loads(source_bytes)
    original = source_snapshot(source)
    if (destination == base or destination.is_relative_to(base) or base.is_relative_to(destination)
            or destination == original.parent or destination.is_relative_to(original.parent) or original.is_relative_to(destination)):
        raise ValueError("Fast-story workspace must be separate from source and canonical records")
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("Use a fresh fast-story directory; prior attempts are preserved, not automatically replayed")
    records = [json.loads(line) for line in evidence_bytes.decode("utf-8").splitlines() if line.strip()]
    packet = source_packet(records)
    data = json.dumps(packet, ensure_ascii=False, separators=(",", ":"))
    if len(data) > MAX_INPUT_CHARACTERS:
        raise ValueError("Fast story exceeds the complete-source size limit; no records were truncated")
    language_info = resolve_language(packet, language)
    contract = (PROMPTS / "fast-story.md").read_text(encoding="utf-8")
    prompt = (contract + "\nOUTPUT_LANGUAGE\n" + json.dumps(language_info, ensure_ascii=False)
              + reference_role_guidance(packet) + "\nHANDOFF_ATTENTION_CANDIDATES\n"
              + json.dumps(handoff_attention(packet), ensure_ascii=False) + "\nHUMAN_INPUT_REFS\n"
              + json.dumps([event["ref"] for event in packet if event.get("human_input")])
              + "\nCOMPLETE_OBSERVABLE_SOURCE_DATA\n" + data)
    identity = {"profile": PROFILE, "source_sha256": source["source_sha256"], "canonical_source_sha256": digest(source_bytes),
                "canonical_evidence_sha256": digest(evidence_bytes), "input_sha256": digest(data.encode()),
                "contract_sha256": digest(contract.encode()), "model": model or getattr(backend, "model", None) or "copilot-default",
                "requested_language": language, "evidence_renderer": EVIDENCE_RENDERER}
    receipt = {"schema": SCHEMA, "identity": identity, "status": "running", "semantic_review": "pending",
               "max_generation_calls": 2, "attempts": [], "calls": [], "prompt_characters": len(prompt)}
    first_call = len(backend.calls)
    with export_lock(destination), content_redaction(False):
        support = destination / "_support"
        support.mkdir()
        (support / "source.json").write_bytes(source_bytes)
        (support / "evidence.jsonl").write_bytes(evidence_bytes)
        (support / "fast-contract.md").write_bytes(contract.encode())
        write_json(support / "input.json", packet)
        write_json(support / "language.json", language_info)
        write_json(support / "fast-attempt.json", receipt)
        try:
            repair_mode = None
            draft_issues = []
            for attempt in range(2):
                label = "fast-story-draft" if attempt == 0 else "fast-story-repair"
                entry = {"index": attempt, "label": label}
                if attempt:
                    entry["repair_mode"] = repair_mode
                receipt["attempts"].append(entry)
                write_json(support / "fast-attempt.json", receipt)
                try:
                    response = backend.generate(prompt, label)
                    response_path = support / f"fast-response-{attempt}.json"
                    write_json(response_path, response)
                    entry["raw_response_sha256"] = file_hash(response_path)
                    if attempt and repair_mode == "field_replacements":
                        repair_path = support / "fast-replacements-1.json"
                        write_json(repair_path, response)
                        entry["response_sha256"] = file_hash(repair_path)
                        try:
                            edition = apply_replacements(edition, response)
                        except (ValueError, TypeError) as error:
                            entry["issues"] = [str(error)]
                            raise DraftValidationError("Fast draft failed its one bounded structural repair; invalid replacements: " + str(error), draft_issues) from error
                    else:
                        edition, moves = normalize_edition_envelope(response)
                        if moves:
                            entry["envelope_moves"] = moves
                    write_json(support / f"fast-candidate-{attempt}.json", edition)
                    issues = structural_issues(edition, packet, language)
                    entry.update(candidate_sha256=file_hash(support / f"fast-candidate-{attempt}.json"), issues=issues)
                    repair_data = {"candidate": edition, "issues": issues}
                except ModelResponseError as error:
                    issues = ["Invalid JSON response; return one complete object with escaped strings"]
                    repair_data = {"invalid_response": error.response, "issues": issues}
                    entry["issues"] = issues
                    write_json(support / f"fast-invalid-response-{attempt}.json", repair_data)
                if not issues:
                    break
                write_json(support / "fast-attempt.json", receipt)
                if attempt:
                    raise DraftValidationError("Fast draft failed its one bounded structural repair; no output approved", [*draft_issues, *issues])
                draft_issues = issues
                patchable = (isinstance(repair_data.get("candidate"), dict)
                             and set(repair_data["candidate"]) == {"article", "brief", "insights"}
                             and isinstance(repair_data["candidate"].get("article"), dict)
                             and not {"brief", "insights"} & repair_data["candidate"]["article"].keys())
                repair_mode = "field_replacements" if patchable else "complete_json"
                repair_instruction = (
                    'Return only {"replacements":[{"path":["article","agent_detail","continuation",0,"steps",0,"refs"],"value":["ACTUAL_SOURCE_REF"]}]}. '
                    "This shape is illustrative, not an instruction to change that field or use that placeholder. "
                    "Replace only fields needed for the listed defects; do not regenerate the draft. "
                    "Use 1–8 edits and at most 6,000 JSON characters total. Paths are arrays of exact existing keys and zero-based integer indices, "
                    "beneath article/brief/insights; never replace those whole roots. No add/remove operations, duplicate or overlapping paths. "
                    "If a required field is missing, replace its nearest EXISTING parent object with that object plus the source-supported field, "
                    "preserving every other field and value; never target a missing key directly. Error locations use slash-separated paths; "
                    "convert array indices to integers in replacement paths. "
                    "Every value and reference must be justified by the complete source. Never delete requirements, coverage or a continuation to evade a defect. "
                    if repair_mode == "field_replacements" else
                    "The draft has an invalid JSON/root envelope that field replacements cannot fix; return one complete corrected joint JSON object within the 12,000-character target, not replacements. "
                )
                prompt += ("\nONE_FINAL_STRUCTURAL_REPAIR\n" + json.dumps(repair_data, ensure_ascii=False)
                           + "\n" + repair_instruction + "Preserve facts and all human input refs. "
                             "Change only structural defects; Human prose has no E IDs, Agent citations remain. No further drafting call is available.")
            source_snapshot(source)
            if plain_path(base / "source.json").read_bytes() != source_bytes or plain_path(base / "evidence.jsonl").read_bytes() != evidence_bytes:
                raise ValueError("Canonical source changed during drafting")
            for name in ("article", "brief", "insights"):
                write_json(support / (name + ".json"), edition[name])
            write_json(support / "edition.json", edition)
            write_json(support / "human-presentation.json", HUMAN_PRESENTATION)
            write_json(support / "tool-ledger.json", tool_ledger(packet))
            write_agent_package(edition["article"], packet, language_info["language"], destination, support, evidence_renderer=EVIDENCE_RENDERER)
            render_story(support, destination / "human-spec.html")
            receipt.update(status=STATUS, output_sha256=file_hash(support / "edition.json"), calls=copy.deepcopy(backend.calls[first_call:]))
            write_json(support / "fast-receipt.json", receipt)
            report = {"schema": SCHEMA, "profile": PROFILE, "status": STATUS, "identity": identity, "semantic_review": "pending",
                      "language": language_info["language"], "evidence_renderer": EVIDENCE_RENDERER, "source_sha256": source["source_sha256"],
                      "hashes": {name: file_hash(destination / name) for name in sorted(OUTPUT_FILES)},
                      "support_hashes": {name: file_hash(support / name) for name in sorted(SUPPORT_FILES)},
                      "limitations": ["Structural validation only. Final source-grounded quality/privacy review is still required.",
                                      "No fresh-reader probe, receiver execution or project tests were run."]}
            write_json(support / "story-report.json", report)
            return {"status": STATUS, "profile": PROFILE, "output": str(destination), "model_calls": len(receipt["calls"]),
                    "prompt_characters": receipt["prompt_characters"], "semantic_review": "pending"}
        except BaseException as error:
            receipt.update(status="failed", failure_type=type(error).__name__)
            raise
        finally:
            receipt["calls"] = copy.deepcopy(backend.calls[first_call:])
            write_json(support / "fast-attempt.json", receipt)


def validate_fast_story(directory):
    errors = []
    try:
        directory = plain_path(directory)
        support = plain_path(directory / "_support")
        report = json.loads(plain_path(support / "story-report.json").read_bytes())
        if (report.get("schema") != SCHEMA or report.get("profile") != PROFILE or report.get("status") != STATUS
                or report.get("semantic_review") != "pending" or report.get("evidence_renderer") != EVIDENCE_RENDERER
                or set(report.get("hashes", {})) != OUTPUT_FILES or set(report.get("support_hashes", {})) != SUPPORT_FILES):
            raise ValueError("Invalid fast-story structural-only publication policy")
        for root, hashes in ((directory, report["hashes"]), (support, report["support_hashes"])):
            for name, expected in hashes.items():
                if file_hash(plain_path(root / name)) != expected:
                    raise ValueError("Fast-story bound artifact changed: " + name)
        source_bytes = (support / "source.json").read_bytes()
        evidence_bytes = (support / "evidence.jsonl").read_bytes()
        source = json.loads(source_bytes)
        source_snapshot(source)
        records = [json.loads(line) for line in evidence_bytes.decode("utf-8").splitlines() if line.strip()]
        packet = source_packet(records)
        if json.loads((support / "input.json").read_bytes()) != packet:
            raise ValueError("Fast-story input differs from complete canonical evidence")
        identity = report["identity"]
        receipt = json.loads((support / "fast-receipt.json").read_bytes())
        if (identity.get("profile") != PROFILE or identity.get("source_sha256") != source["source_sha256"]
                or identity.get("canonical_source_sha256") != digest(source_bytes)
                or identity.get("canonical_evidence_sha256") != digest(evidence_bytes)
                or identity.get("input_sha256") != digest(json.dumps(packet, ensure_ascii=False, separators=(",", ":")).encode())
                or identity.get("contract_sha256") != file_hash(support / "fast-contract.md")
                or identity.get("evidence_renderer") != EVIDENCE_RENDERER or receipt.get("identity") != identity
                or receipt.get("schema") != SCHEMA or receipt.get("status") != STATUS or receipt.get("semantic_review") != "pending"
                or receipt.get("max_generation_calls") != 2 or not 1 <= len(receipt.get("attempts", [])) <= 2
                or len(receipt.get("calls", [])) != len(receipt["attempts"])):
            raise ValueError("Fast-story generation receipt is inconsistent")
        for entry in receipt["attempts"]:
            if "raw_response_sha256" not in entry:
                continue
            response_path = plain_path(support / f"fast-response-{entry['index']}.json")
            if file_hash(response_path) != entry["raw_response_sha256"]:
                raise ValueError("Fast-story raw model response changed")
            if "envelope_moves" in entry:
                normalized, moves = normalize_edition_envelope(json.loads(response_path.read_bytes()))
                candidate_path = plain_path(support / f"fast-candidate-{entry['index']}.json")
                if (moves != entry["envelope_moves"] or normalized != json.loads(candidate_path.read_bytes())
                        or file_hash(candidate_path) != entry["candidate_sha256"]):
                    raise ValueError("Fast-story envelope normalization differs from its raw response")
        edition = json.loads((support / "edition.json").read_bytes())
        if receipt.get("output_sha256") != file_hash(support / "edition.json"):
            raise ValueError("Fast draft differs from its structural receipt")
        if edition != {name: json.loads((support / (name + ".json")).read_bytes()) for name in ("article", "brief", "insights")}:
            raise ValueError("Fast-story rendered components differ from the draft")
        language = json.loads((support / "language.json").read_bytes())
        if language != resolve_language(packet, identity["requested_language"]) or language["language"] != report["language"]:
            raise ValueError("Fast-story language binding changed")
        errors.extend(structural_issues(edition, packet, identity["requested_language"]))
        if errors:
            return {"valid": False, "issues": errors, "semantic_review": "pending"}
        if (json.loads((support / "agent-presentation.json").read_bytes()) != PRESENTATION
                or json.loads((support / "human-presentation.json").read_bytes()) != HUMAN_PRESENTATION
                or json.loads((support / "tool-ledger.json").read_bytes()) != tool_ledger(packet)):
            raise ValueError("Fast-story presentation or tool ledger changed")
        with content_redaction(False):
            outputs = render_agent_package(edition["article"], packet, report["language"], evidence_renderer=EVIDENCE_RENDERER)
            for name, text in outputs.items():
                alias = "agent-rendered.md" if name == "agent-spec.md" else "evidence-rendered.md"
                if (directory / name).read_bytes() != text.encode("utf-8") or (support / alias).read_bytes() != text.encode("utf-8"):
                    raise ValueError("Fast-story delivered Markdown differs from the structural draft")
            with tempfile.TemporaryDirectory(prefix="fast-story-verify-") as temporary:
                target = Path(temporary) / "human-spec.html"
                render_story(support, target)
                if target.read_bytes() != (directory / "human-spec.html").read_bytes():
                    raise ValueError("Fast-story HTML differs from the structural draft")
    except PreparationTimeoutError:
        raise
    except (OSError, ValueError, KeyError, TypeError, AttributeError, UnicodeError) as error:
        errors.append(str(error))
    return {"valid": not errors, "issues": errors, "status": STATUS, "semantic_review": "pending",
            "note": "Structural consistency only; source-grounded quality/privacy review is required before delivery."}
