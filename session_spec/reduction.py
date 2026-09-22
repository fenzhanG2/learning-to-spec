import copy
import base64
import hashlib
import html
import json
import re
import urllib.parse
from collections import Counter
from pathlib import Path

from .ingest import read_session, resolve_session
from .privacy import HIDDEN_FIELDS, SECRET_FIELD, SECRET_PATTERNS, sanitize, content_redaction
from .reduction_rules import CATEGORIES, detect
from .disclosure_context import local_combinations
from .storage import file_hash, write_json


LIMIT = 32 * 1024 * 1024
ACTIONS = {"keep", "remove", "pseudonymize", "generalize"}
CONTEXT_SCOPE = "baseline-field/v2"
STRUCTURAL = {"id", "type", "parentId", "sessionId", "toolCallId", "toolName", "agentId", "parentAgentTaskId", "parentToolCallId", "source", "sourceTurnId", "timestamp", "version"}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def strings(value, path=()):
    if isinstance(value, dict):
        for key, child in value.items():
            if key not in STRUCTURAL or len(path) > 2:
                yield from strings(child, (*path, key))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from strings(child, (*path, index))
    elif isinstance(value, str) and value:
        yield path, value


def at_path(value, path):
    current = value
    for key in path:
        current = current[key]
    return current


def set_path(value, path, text):
    parent = at_path(value, path[:-1])
    parent[path[-1]] = text


def source_snapshot(path):
    if path.stat().st_size > LIMIT:
        raise ValueError("Privacy review supports at most 32 MiB of session input; no silent truncation")
    content = path.read_bytes()
    events = []
    for number, line in enumerate(content.splitlines(), 1):
        if not line.strip():
            events.append({})
            continue
        try:
            event = json.loads(line.decode("utf-8-sig"))
        except (ValueError, UnicodeError):
            raise ValueError(f"Incomplete or invalid session event at line {number}; finish the session before review") from None
        if not isinstance(event, dict) or not isinstance(event.get("data", {}), dict):
            raise ValueError(f"Invalid session event at line {number}")
        events.append(event)
    if not events:
        raise ValueError("Empty session")
    return events, hashlib.sha256(content).hexdigest()


def hard_counts(value):
    counts = Counter()
    if isinstance(value, dict):
        for key, child in value.items():
            if key in HIDDEN_FIELDS:
                counts["hidden_or_binary_fields"] += 1
            elif SECRET_FIELD.fullmatch(key):
                counts["secret_fields"] += 1
            else:
                counts.update(hard_counts(child))
    elif isinstance(value, list):
        for child in value:
            counts.update(hard_counts(child))
    elif isinstance(value, str):
        counts["secret_pattern_matches"] += sum(len(pattern.findall(value)) for pattern in SECRET_PATTERNS)
    return counts


def secure_baseline(value):
    value = sanitize(value)
    if isinstance(value, dict):
        return {key: secure_baseline(child) for key, child in value.items()}
    if isinstance(value, list):
        return [secure_baseline(child) for child in value]
    if not isinstance(value, str):
        return value

    def replace_encoded(match):
        encoded = match.group()
        candidates = [urllib.parse.unquote(encoded), html.unescape(encoded)]
        try:
            candidates.append(base64.b64decode(encoded + "=" * (-len(encoded) % 4), validate=True).decode("utf-8"))
        except (ValueError, UnicodeError):
            pass
        try:
            candidates.append(bytes.fromhex(encoded).decode("utf-8"))
        except (ValueError, UnicodeError):
            pass
        for decoded in candidates:
            if decoded != encoded and sanitize(decoded) != decoded:
                return "[REDACTED]"
        return encoded

    return re.sub(r"[^\s<>\"']*%[0-9a-fA-F]{2}[^\s<>\"']*|[A-Za-z0-9+/=]{24,4096}|[^\s<>\"']*&#(?:\d+|x[0-9a-fA-F]+);[^\s<>\"']*", replace_encoded, value)


def canonical_items(items):
    return sorted({digest(item): item for item in items}.values(), key=digest)


def context_identity(literal, field_hash):
    return "PC" + digest([CONTEXT_SCOPE, literal, field_hash])


def summarize_context(finding):
    local = finding["local_judgments"]
    assessments = finding["assessments"]
    judgments = assessments or local
    categories = sorted({item["category"] for item in local + assessments})
    necessities = {item["necessity"] for item in judgments}
    alternatives = {item["alternative"] for item in judgments}
    reasons = ["Local: " + item["reason"] for item in local]
    reasons += ["Contextual: " + item["reason"] for item in assessments]
    reason = "\n".join(dict.fromkeys(reasons))
    if len(necessities) > 1:
        reason = "Conflicting necessity assessments; choose for this exact context.\n" + reason
    finding.update(category=categories[0], categories=categories,
                   label=" / ".join(CATEGORIES[category]["label"] for category in categories),
                   necessity=next(iter(necessities)) if len(necessities) == 1 else "uncertain",
                   alternative=next(iter(alternatives)) if len(alternatives) == 1 and len(necessities) == 1 else "",
                   related=sorted({slot for item in local + assessments for slot in item["related"]}),
                   detectors=sorted({detector for item in local for detector in item["detectors"]} | ({"copilot"} if assessments else set())),
                   recommended=None, reason=reason)


def add_context_finding(findings, text, path, start, end, category, detector, reason, necessity,
                        alternative, related, source_finding=None, provenance=None):
    literal = text[start:end]
    field_hash = digest(text)
    identifier = context_identity(literal, field_hash)
    finding = findings.setdefault(identifier, {"id": identifier, "text": literal,
        "scope": {"schema": CONTEXT_SCOPE, "field_sha256": field_hash},
        "occurrences": [], "local_judgments": [], "assessments": []})
    occurrence = {"path": list(path), "start": start, "end": end, "field_sha256": field_hash}
    finding["occurrences"] = canonical_items([*finding["occurrences"], occurrence])
    if detector == "copilot":
        assessment = {"category": category, "necessity": necessity, "reason": reason,
                      "alternative": alternative, "related": sorted(set(related or []))}
        assessment_id = digest(assessment)
        saved = next((item for item in finding["assessments"] if item["id"] == assessment_id), None)
        if saved is None:
            saved = {**assessment, "id": assessment_id, "sources": [], "occurrences": []}
            finding["assessments"].append(saved)
        saved["occurrences"] = canonical_items([*saved["occurrences"], occurrence])
        if provenance is not None:
            saved["sources"] = canonical_items([*saved["sources"], copy.deepcopy(provenance)])
        finding["assessments"].sort(key=lambda item: item["id"])
    else:
        original = source_finding or {"id": "P" + digest([category, literal])[:16], "category": category,
            "reason": reason or CATEGORIES[category]["why"], "necessity": necessity,
            "alternative": alternative, "related": related or [], "detectors": [detector], "recommended": None}
        judgment = {key: copy.deepcopy(original[key]) for key in
                    ("category", "reason", "necessity", "alternative", "related", "detectors", "recommended")}
        judgment["source_finding_id"] = original["id"]
        saved = next((item for item in finding["local_judgments"] if item["source_finding_id"] == original["id"]), None)
        if saved is None:
            saved = {**judgment, "occurrences": []}
            finding["local_judgments"].append(saved)
        saved["occurrences"] = canonical_items([*saved["occurrences"], occurrence])
        finding["local_judgments"].sort(key=lambda item: item["source_finding_id"])
    summarize_context(finding)


def scoped_candidates(review, baseline):
    if review["schema"] == "privacy-review/v2":
        return {finding["id"]: copy.deepcopy(finding) for finding in review["findings"]}
    if review.get("semantic", {}).get("status") != "not_run" or any(
            "copilot" in finding.get("detectors", []) or "scope" in finding or "assessments" in finding
            for finding in review["findings"]):
        raise ValueError("Legacy semantic reviews require a fresh scan; old audit and choices cannot be migrated")
    candidates = {}
    for finding in review["findings"]:
        if finding["category"] == "custom":
            candidates[finding["id"]] = copy.deepcopy(finding)
            continue
        for occurrence in finding["occurrences"]:
            text = at_path(baseline, occurrence["path"])
            start, end = occurrence["start"], occurrence["end"]
            if digest(text) != occurrence["field_sha256"] or text[start:end] != finding["text"]:
                raise ValueError("Privacy span no longer matches the source")
            add_finding(candidates, text, occurrence["path"], start, end, finding["category"],
                        context_scoped=True, source_finding=finding)
    return candidates


def validate_context_review(review, baseline):
    if review.get("semantic", {}).get("finding_scope") != CONTEXT_SCOPE:
        raise ValueError("Unknown contextual privacy scope; scan again")
    identifiers = set()
    for finding in review["findings"]:
        if finding["id"] in identifiers:
            raise ValueError("Duplicate contextual finding identity")
        identifiers.add(finding["id"])
        if "scope" not in finding:
            if finding["category"] != "custom" or "copilot" in finding.get("detectors", []):
                raise ValueError("Automatic v2 findings must have a contextual scope")
            continue
        if not finding.get("occurrences") or not (finding.get("local_judgments") or finding.get("assessments")):
            raise ValueError("Contextual findings require source occurrences and judgments")
        text = at_path(baseline, finding["occurrences"][0]["path"])
        field_hash = digest(text)
        if finding["scope"] != {"schema": CONTEXT_SCOPE, "field_sha256": field_hash} or finding["id"] != context_identity(finding["text"], field_hash):
            raise ValueError("Contextual finding identity does not match its field")
        occurrences = {digest(item) for item in finding["occurrences"]}
        for occurrence in finding["occurrences"]:
            original = at_path(baseline, occurrence["path"])
            if not 0 <= occurrence["start"] < occurrence["end"] <= len(original) or original != text or occurrence["field_sha256"] != field_hash or original[occurrence["start"]:occurrence["end"]] != finding["text"]:
                raise ValueError("Contextual occurrence does not match its field")
        evidence = finding["local_judgments"] + finding["assessments"]
        for judgment in evidence:
            if judgment["category"] not in CATEGORIES or judgment["category"] == "custom" or judgment["necessity"] not in {"necessary", "unnecessary", "uncertain"}:
                raise ValueError("Invalid contextual judgment")
        for assessment in finding["assessments"]:
            payload = {key: assessment[key] for key in ("category", "necessity", "reason", "alternative", "related")}
            if assessment["id"] != digest(payload):
                raise ValueError("Contextual assessment identity changed")
        if {digest(item) for judgment in evidence for item in judgment["occurrences"]} != occurrences:
            raise ValueError("Contextual occurrence provenance is incomplete")
        expected = copy.deepcopy(finding)
        summarize_context(expected)
        if expected != finding:
            raise ValueError("Contextual assessment summary changed; scan again")


def validate_context_provenance(review, baseline, ancestors):
    slots = {}
    seen = set()
    for number, (path, text) in enumerate(strings(baseline), 1):
        if text not in seen:
            slots[f"S{number}"] = (list(path), text)
            seen.add(text)
    originals = {finding["id"]: finding for ancestor in ancestors.values()
                 if ancestor["schema"] == "privacy-review/v1" for finding in ancestor["findings"]}
    for finding in review["findings"]:
        if "scope" not in finding:
            continue
        field = at_path(baseline, finding["occurrences"][0]["path"])
        for judgment in finding["local_judgments"]:
            source_id = judgment.get("source_finding_id")
            original = originals.get(source_id) if isinstance(source_id, str) else None
            keys = ("category", "reason", "necessity", "alternative", "related", "detectors", "recommended")
            if original is None or original["text"] != finding["text"] or any(judgment.get(key) != original.get(key) for key in keys):
                raise ValueError("Local judgment provenance does not match its parent finding")
            expected = canonical_items([occurrence for occurrence in original["occurrences"]
                                        if occurrence["field_sha256"] == finding["scope"]["field_sha256"]])
            if judgment["occurrences"] != expected:
                raise ValueError("Local judgment occurrences do not match their parent partition")
        for assessment in finding["assessments"]:
            sources = assessment.get("sources")
            if not isinstance(sources, list) or not sources:
                raise ValueError("Contextual assessment requires source provenance")
            for source in sources:
                if not isinstance(source, dict) or set(source) != {"review_id", "pass", "slot", "path"}:
                    raise ValueError("Invalid contextual source provenance")
                if not isinstance(source["review_id"], str) or source["review_id"] not in ancestors:
                    raise ValueError("Contextual source review is not a bound ancestor")
                if not isinstance(source["pass"], str) or not re.fullmatch(r"privacy-(?:context-[1-9][0-9]*|cross-context)", source["pass"]):
                    raise ValueError("Invalid contextual source pass")
                slot = slots.get(source["slot"]) if isinstance(source["slot"], str) else None
                if slot is None or source["path"] != slot[0] or slot[1] != field:
                    raise ValueError("Contextual source slot does not match its field")
            if not isinstance(assessment["related"], list) or any(not isinstance(slot, str) or slot not in slots for slot in assessment["related"]):
                raise ValueError("Invalid contextual related source slots")


def add_finding(findings, text, path, start, end, category, detector="local", reason=None, necessity="uncertain", alternative="", related=None, context_scoped=False, source_finding=None, provenance=None):
    if category not in CATEGORIES or not 0 <= start < end <= len(text):
        raise ValueError("Invalid privacy finding")
    if context_scoped:
        add_context_finding(findings, text, path, start, end, category, detector, reason, necessity,
                            alternative, related, source_finding, provenance)
        return
    literal = text[start:end]
    identifier = "P" + digest([category, literal])[:16]
    finding = findings.setdefault(identifier, {
        "id": identifier, "category": category, "label": CATEGORIES[category]["label"],
        "text": literal, "reason": reason or CATEGORIES[category]["why"],
        "necessity": necessity, "recommended": CATEGORIES[category]["recommend"] if category in {"identifier", "environment", "custom"} else None,
        "alternative": alternative, "detectors": [], "occurrences": [], "related": related or [],
    })
    occurrence = {"path": list(path), "start": start, "end": end, "field_sha256": digest(text)}
    if occurrence not in finding["occurrences"]:
        finding["occurrences"].append(occurrence)
    if detector not in finding["detectors"]:
        finding["detectors"].append(detector)
    if detector == "copilot":
        finding.update(reason=reason, necessity=necessity, alternative=alternative, related=related or [])
        if necessity != "unnecessary":
            finding["recommended"] = None
    if "\n" in literal or len(literal) > 240:
        finding["recommended"] = None


def review_identity(review):
    keys = ("schema", "source_sha256", "baseline_sha256", "purpose", "audience", "findings", "semantic")
    keys += ("preferences", "privacy_mode", "pipeline", "abstraction_sha256")
    if review.get("schema") == "privacy-review/v2":
        keys += ("parent_review",)
    return digest({key: review[key] for key in keys if key in review})


def prepare_full_session(session, home, output, audience, preferences):
    from .delivery import preferences as validate_preferences
    selection = validate_preferences(preferences.get("readers"), preferences.get("destination"), audience)
    source = resolve_session(session, Path(home))
    output = Path(output).expanduser().resolve()
    if output == source.parent or output.is_relative_to(source.parent) or output.is_relative_to(Path(home).resolve()):
        raise ValueError("Export approval must be outside the source session and Copilot home")
    if output.exists() and any(output.iterdir()):
        raise ValueError("Use a new empty export approval directory")
    events, source_hash = source_snapshot(source)
    with content_redaction(False):
        baseline = sanitize(events)
    output.mkdir(parents=True, exist_ok=True)
    write_json(output / "baseline.json", baseline)
    review = {"schema": "privacy-review/v1", "privacy_mode": "full", "source_path": str(source),
              "source_sha256": source_hash, "baseline_sha256": file_hash(output / "baseline.json"),
              "purpose": "Technical story and actionable Agent handoff", "audience": audience,
              "preferences": selection, "hard_removals": {}, "findings": [],
              "semantic": {"status": "skipped_by_choice", "coverage": "No privacy scan or sensitive-content redaction requested."},
              "limitations": ["Personal, internal and credential content may enter the generated files.",
                              "Only observable conversation data is included; hidden/control data remains excluded.",
                              "Generating a spec still uses Copilot and summarizes rather than reproducing every message.",
                              "ArtifactStore publication retains its separate final-file safety checks and confirmation."]}
    review["review_id"] = review_identity(review)
    write_json(output / "review.json", review)
    return review


def scan_session(session, home, output, purpose="Technical story and actionable Agent handoff", audience="local", custom=None, preferences=None, privacy_mode=None):
    if privacy_mode not in {None, "llm"}:
        raise ValueError("Scanning requires the smart-redaction mode")
    if not purpose.strip() or audience != "local" and audience != "root" and not re.fullmatch(r"team:[A-Za-z0-9][A-Za-z0-9_-]{0,99}", audience):
        raise ValueError("Provide a purpose and audience: local, root or team:SLUG")
    source = resolve_session(session, Path(home))
    output = Path(output).expanduser().resolve()
    if output == source.parent or output.is_relative_to(source.parent) or output.is_relative_to(Path(home).resolve()):
        raise ValueError("Review directory must be outside the source session and Copilot home")
    if output.exists() and any(output.iterdir()):
        raise ValueError("Use a new empty privacy review directory")
    events, source_hash = source_snapshot(source)
    counts = hard_counts(events)
    baseline = secure_baseline(events)
    counts["encoded_secret_fields"] = sum(1 for path, text in strings(sanitize(events)) if at_path(baseline, path) != text)
    findings = {}
    for path, text in strings(baseline):
        for start, end, category in detect(text):
            add_finding(findings, text, path, start, end, category)
        for phrase in custom or []:
            if not isinstance(phrase, str) or len(phrase.strip()) < 2:
                raise ValueError("Additional redactions must contain at least two characters")
            for match in re.finditer(re.escape(phrase), text):
                add_finding(findings, text, path, *match.span(), "custom")
    user_fields = [(f"S{number}", path, text) for number, (path, text) in enumerate(strings(baseline), 1)
                   if baseline[path[0]].get("type") == "user.message" and path[1:] in {("data", "content"), ("data", "text")}]
    for clue in local_combinations(user_fields):
        text = at_path(baseline, clue["path"])
        add_finding(findings, text, clue["path"], clue["start"], clue["end"], "inference",
                    detector="local-combination", necessity="uncertain", related=clue["related"],
                    reason="Personal context from different turns may be linkable when combined. Review these clues together for this audience; no identity or private attribute was inferred. Up to twelve related fields are shown; this heuristic is not exhaustive.")
    output.mkdir(parents=True, exist_ok=True)
    write_json(output / "baseline.json", baseline)
    review = {"schema": "privacy-review/v1", "source_path": str(source), "source_sha256": source_hash,
              "baseline_sha256": file_hash(output / "baseline.json"), "purpose": purpose.strip(), "audience": audience,
              "hard_removals": dict(counts), "findings": list(findings.values()),
              "semantic": {"status": "not_run", "coverage": "Local direct-pattern and cross-turn personal-context heuristics only; unrecognized, quoted, third-party or semantic combinations may still be missed. No identity inference or automatic contextual removal."},
              "limitations": ["Not an anonymization or compliance guarantee.", "Review text is private local data; never upload this directory.", "Technical failures and acceptance boundaries must remain in the handoff."]}
    if preferences is not None:
        from .delivery import preferences as validate_preferences
        review["preferences"] = validate_preferences(preferences.get("readers"), preferences.get("destination"), audience)
    if privacy_mode is not None:
        review["privacy_mode"] = privacy_mode
    review["review_id"] = review_identity(review)
    write_json(output / "review.json", review)
    return review


def load_review(directory):
    directory = Path(directory).resolve()
    review = json.loads((directory / "review.json").read_bytes())
    if review.get("privacy_mode") not in {None, "full", "llm"}:
        raise ValueError("Unknown saved privacy mode")
    if review.get("schema") not in {"privacy-review/v1", "privacy-review/v2"} or review.get("review_id") != review_identity(review):
        raise ValueError("Privacy review changed; scan again")
    if file_hash(directory / "baseline.json") != review["baseline_sha256"] or file_hash(Path(review["source_path"])) != review["source_sha256"]:
        raise ValueError("Session or privacy baseline changed; old choices are no longer valid")
    baseline = json.loads((directory / "baseline.json").read_bytes())
    if review["schema"] == "privacy-review/v2":
        ancestor = review
        ancestors = {}
        visited = set()
        while ancestor["schema"] == "privacy-review/v2":
            parent = ancestor.get("parent_review", {})
            fingerprint = parent.get("sha256", "")
            if not re.fullmatch(r"[0-9a-f]{64}", fingerprint) or fingerprint in visited:
                raise ValueError("Contextual review requires a bound parent audit")
            visited.add(fingerprint)
            parent_path = directory / "audit" / f"review-{fingerprint}.json"
            if not parent_path.is_file() or file_hash(parent_path) != fingerprint:
                raise ValueError("Parent privacy audit changed or is missing")
            original = json.loads(parent_path.read_bytes())
            if original.get("schema") not in {"privacy-review/v1", "privacy-review/v2"} or original.get("review_id") != parent.get("review_id") or review_identity(original) != parent["review_id"]:
                raise ValueError("Parent privacy audit identity changed")
            if any(original.get(key) != review.get(key) for key in ("source_sha256", "baseline_sha256", "purpose", "audience")):
                raise ValueError("Parent privacy audit belongs to a different source or audience")
            ancestors[original["review_id"]] = original
            ancestor = original
        validate_context_review(review, baseline)
        validate_context_provenance(review, baseline, ancestors)
    return review, baseline


def suggested_action(finding):
    if "scope" in finding:
        return None
    if finding.get("category") not in {"identifier", "environment", "custom"}:
        return None
    literal = finding.get("text", "")
    if "\n" in literal or "\r" in literal or len(literal) > 240:
        return None
    if "copilot" in finding.get("detectors", []) and finding.get("necessity") != "unnecessary":
        return None
    recommendation = finding.get("recommended")
    return recommendation if recommendation in {"remove", "pseudonymize"} else None


def recommended_decisions(review):
    return {"review_id": review["review_id"], "audience": review["audience"],
            "choices": {finding["id"]: {"action": action} for finding in review["findings"] if (action := suggested_action(finding))}}


def transform(review, baseline, decisions):
    if review.get("privacy_mode") == "llm" and review.get("semantic", {}).get("status") != "reviewed":
        raise ValueError("Smart redaction requires completed Copilot review; local rules alone are not sufficient")
    if decisions.get("review_id") != review["review_id"] or decisions.get("audience") != review["audience"]:
        raise ValueError("Decisions must match this review and intended audience")
    choices = decisions.get("choices", {})
    if not isinstance(choices, dict) or set(choices) != {finding["id"] for finding in review["findings"]}:
        raise ValueError("Every finding needs an explicit choice; unknown or missing decisions are refused")
    if review.get("privacy_mode") == "full":
        if review["findings"] or review["semantic"].get("status") != "skipped_by_choice":
            raise ValueError("Full-content approval has inconsistent privacy state")
        return copy.deepcopy(baseline), {}
    contextual = review.get("schema") == "privacy-review/v2"
    if contextual:
        validate_context_review(review, baseline)
    aliases = {literal: number for number, literal in enumerate(sorted({finding["text"] for finding in review["findings"]}), 1)} if contextual else {}
    replacements = {}
    kept = {}
    operations = Counter()
    for number, finding in enumerate(review["findings"], 1):
        choice = choices[finding["id"]]
        action = choice.get("action") if isinstance(choice, dict) else None
        if action not in ACTIONS:
            raise ValueError("Invalid privacy action")
        operations[action] += len(finding["occurrences"])
        if action == "keep":
            for occurrence in finding["occurrences"]:
                kept.setdefault(tuple(occurrence["path"]), []).append((occurrence["start"], occurrence["end"]))
            continue
        marker = aliases[finding["text"]] if contextual else number
        replacement = f"[PRIVATE_DETAIL_{marker}]" if action == "remove" else f"[ENTITY_{marker}]"
        if action == "generalize":
            replacement = choice.get("replacement", "").strip()
            if not replacement or len(replacement) > 500 or replacement == finding["text"] or sanitize(replacement) != replacement:
                raise ValueError("Generalization needs a non-secret, different replacement under 500 characters")
            replacement = f"[GENERALIZED_DETAIL_{marker}: {replacement}]"
        for occurrence in finding["occurrences"]:
            path = tuple(occurrence["path"])
            text = at_path(baseline, path)
            start, end = occurrence["start"], occurrence["end"]
            if digest(text) != occurrence["field_sha256"] or text[start:end] != finding["text"]:
                raise ValueError("Privacy span no longer matches the source")
            replacements.setdefault(path, []).append((start, end, replacement, finding["id"]))
    result = copy.deepcopy(baseline)
    for path, spans in replacements.items():
        if any(start < kept_end and end > kept_start for start, end, replacement, identifier in spans for kept_start, kept_end in kept.get(path, [])):
            raise ValueError("A removal overlaps a detail you chose to keep. Resolve these conflicting choices before generation.")
        if contextual:
            exact = {}
            for start, end, replacement, identifier in spans:
                if (start, end) in exact and exact[start, end] != replacement:
                    raise ValueError("Incompatible privacy actions on the same exact span")
                exact[start, end] = replacement
            active_ends = []
            for start, end in sorted(exact, key=lambda span: (span[0], -span[1])):
                active_ends = [previous_end for previous_end in active_ends if previous_end > start]
                if any(previous_end < end for previous_end in active_ends):
                    raise ValueError("Partially overlapping privacy edits need a single combined selection")
                active_ends.append(end)
        ordered = sorted(spans, key=lambda span: (span[0], -span[1], span[3]) if contextual else (span[0], -span[1]))
        accepted = []
        for span in ordered:
            if accepted and span[0] < accepted[-1][1]:
                previous = accepted[-1]
                if span[1] <= previous[1]:
                    continue
                raise ValueError("Partially overlapping privacy edits need a single combined selection")
            accepted.append(span)
        text = at_path(result, path)
        for start, end, replacement, identifier in reversed(accepted):
            text = text[:start] + replacement + text[end:]
        set_path(result, path, text)
    if secure_baseline(result) != result:
        raise ValueError("A selected replacement introduced hard-sensitive data")
    return result, dict(operations)


def apply_review(directory, decisions, destination):
    directory = Path(directory).resolve()
    destination = Path(destination).resolve()
    review, baseline = load_review(directory)
    source_directory = Path(review["source_path"]).parent
    if destination == directory or destination.is_relative_to(directory) or destination == source_directory or destination.is_relative_to(source_directory):
        raise ValueError("Reduced session must use a separate new directory")
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("Reduced output already exists; use a new directory")
    reduced, operations = transform(review, baseline, decisions)
    if file_hash(Path(review["source_path"])) != review["source_sha256"]:
        raise ValueError("Session changed during reduction; scan again")
    destination.mkdir(parents=True, exist_ok=True)
    content = "\n".join(json.dumps(event, ensure_ascii=False) for event in reduced) + "\n"
    (destination / "events.jsonl").write_text(content, encoding="utf-8", newline="\n")
    receipt = {"schema": "reduced-session/v1", "review_id": review["review_id"], "decisions_sha256": digest(decisions),
               "source_sha256": review["source_sha256"], "reduced_sha256": file_hash(destination / "events.jsonl"),
               "audience": review["audience"], "purpose": review["purpose"], "hard_removals": review["hard_removals"],
               "operations": operations, "semantic": review["semantic"], "reviewed_findings": len(review["findings"]),
               "note": "Disclosure reduction is not a guarantee of anonymity or semantic completeness."}
    if "preferences" in review:
        receipt["preferences"] = review["preferences"]
    if "privacy_mode" in review:
        receipt["privacy_mode"] = review["privacy_mode"]
    write_json(destination / "reduction.json", receipt)
    write_json(directory / "decisions.json", decisions)
    return receipt


def reduced_export(directory, home):
    directory = Path(directory).resolve()
    receipt = json.loads((directory / "reduction.json").read_bytes())
    if receipt.get("schema") != "reduced-session/v1" or file_hash(directory / "events.jsonl") != receipt["reduced_sha256"]:
        raise ValueError("Reduced session does not match its privacy receipt")
    with content_redaction(receipt.get("privacy_mode") != "full"):
        metadata, records = read_session(directory / "events.jsonl", Path(home))
    export = directory / "canonical"
    export.mkdir(exist_ok=True)
    write_json(export / "source.json", metadata)
    (export / "evidence.jsonl").write_text("\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n", encoding="utf-8")
    return export
