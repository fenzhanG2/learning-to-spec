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
from .privacy import HIDDEN_FIELDS, SECRET_FIELD, SECRET_PATTERNS, sanitize
from .reduction_rules import CATEGORIES, detect
from .disclosure_context import local_combinations
from .storage import file_hash, write_json


LIMIT = 32 * 1024 * 1024
ACTIONS = {"keep", "remove", "pseudonymize", "generalize"}
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


def add_finding(findings, text, path, start, end, category, detector="local", reason=None, necessity="uncertain", alternative="", related=None):
    if category not in CATEGORIES or not 0 <= start < end <= len(text):
        raise ValueError("Invalid privacy finding")
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
    return digest({key: review[key] for key in (*keys, "preferences") if key in review})


def scan_session(session, home, output, purpose="Technical story and actionable Agent handoff", audience="local", custom=None, preferences=None):
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
    review["review_id"] = review_identity(review)
    write_json(output / "review.json", review)
    return review


def load_review(directory):
    directory = Path(directory).resolve()
    review = json.loads((directory / "review.json").read_bytes())
    if review.get("schema") != "privacy-review/v1" or review.get("review_id") != review_identity(review):
        raise ValueError("Privacy review changed; scan again")
    if file_hash(directory / "baseline.json") != review["baseline_sha256"] or file_hash(Path(review["source_path"])) != review["source_sha256"]:
        raise ValueError("Session or privacy baseline changed; old choices are no longer valid")
    return review, json.loads((directory / "baseline.json").read_bytes())


def suggested_action(finding):
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
    if decisions.get("review_id") != review["review_id"] or decisions.get("audience") != review["audience"]:
        raise ValueError("Decisions must match this review and intended audience")
    choices = decisions.get("choices", {})
    if not isinstance(choices, dict) or set(choices) != {finding["id"] for finding in review["findings"]}:
        raise ValueError("Every finding needs an explicit choice; unknown or missing decisions are refused")
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
        replacement = f"[PRIVATE_DETAIL_{number}]" if action == "remove" else f"[ENTITY_{number}]"
        if action == "generalize":
            replacement = choice.get("replacement", "").strip()
            if not replacement or len(replacement) > 500 or replacement == finding["text"] or sanitize(replacement) != replacement:
                raise ValueError("Generalization needs a non-secret, different replacement under 500 characters")
            replacement = f"[GENERALIZED_DETAIL_{number}: {replacement}]"
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
        ordered = sorted(spans, key=lambda span: (span[0], -span[1]))
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
    write_json(destination / "reduction.json", receipt)
    write_json(directory / "decisions.json", decisions)
    return receipt


def reduced_export(directory, home):
    directory = Path(directory).resolve()
    receipt = json.loads((directory / "reduction.json").read_bytes())
    if receipt.get("schema") != "reduced-session/v1" or file_hash(directory / "events.jsonl") != receipt["reduced_sha256"]:
        raise ValueError("Reduced session does not match its privacy receipt")
    metadata, records = read_session(directory / "events.jsonl", Path(home))
    export = directory / "canonical"
    export.mkdir(exist_ok=True)
    write_json(export / "source.json", metadata)
    (export / "evidence.jsonl").write_text("\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n", encoding="utf-8")
    return export
