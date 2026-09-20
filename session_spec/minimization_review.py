import json
import re

from .review_crosswalk import cited_claims
from .source_excerpt import excerpt_segments, payload_text, source_payload
from .story_grounding import field_has_quote, pointer_value


SCHEMA = "minimization-focus/v1"
MAX_FOCUS_CHARS = 32000
MAX_GROUPS = 32
MAX_CLAIMS = 8
MARKER = re.compile(r"\[(?:PRIVATE_DETAIL_\d+|ENTITY_\d+|REDACTED)\]|\[GENERALIZED_DETAIL_\d+:")


def minimization_focus(edition, events):
    claims = cited_claims(edition)
    marked = [(event, payload_text(source_payload(event))) for event in events]
    marked = [(event, text) for event, text in marked if MARKER.search(text)]
    result = {"schema": SCHEMA, "marked_event_count": len(marked), "groups": [], "omitted_groups": 0,
              "limit": "Reduced payloads only, never original removed values or reversal maps. Markers locate possible "
                       "privacy transformations or literal technical examples; they do not prove a disclosure category. "
                       "No keyword is forbidden. Inspect the complete edition for unnecessary paraphrases or mentions "
                       "of removed material, including uncited text. Retain technical failures, corrections and necessary "
                       "operating constraints. This bounded index is not a privacy guarantee or an exhaustive detector."}
    for event, text in marked:
        if len(result["groups"]) >= MAX_GROUPS:
            result["omitted_groups"] += 1
            continue
        matching = [claim for claim in claims if event["ref"] in claim["refs"]]
        surfaces = {}
        for claim in matching:
            parts = claim["path"].split("/")
            depth = 4 if parts[1:3] == ["article", "agent_detail"] else 3
            surfaces.setdefault("/".join(parts[:depth]), []).append(claim)
        selected = []
        while any(surfaces.values()) and len(selected) < MAX_CLAIMS:
            for pending in surfaces.values():
                if pending and len(selected) < MAX_CLAIMS:
                    claim = pending.pop(0)
                    selected.append({"path": claim["path"], "segments": excerpt_segments(claim["quote"], 900),
                                     "characters": len(claim["quote"]), "reference_scope": claim["reference_scope"]})
        windows = []
        for match in MARKER.finditer(text):
            start, end = max(0, match.start() - 200), min(len(text), match.end() + 400)
            if windows and start <= windows[-1]["end"]:
                continue
            windows.append({"start": start, "end": end, "text": text[start:end]})
            if len(windows) == 3:
                break
        group = {"ref": event["ref"], "type": event.get("type"), "source_windows": windows,
                 "source_characters": len(text), "marker_count": len(MARKER.findall(text)),
                 "claims": selected, "omitted_claims": len(matching) - len(selected)}
        result["groups"].append(group)
        if len(json.dumps(result, ensure_ascii=False)) > MAX_FOCUS_CHARS:
            result["groups"].pop()
            result["omitted_groups"] += 1
    while len(json.dumps(result, ensure_ascii=False)) > MAX_FOCUS_CHARS and result["groups"]:
        result["groups"].pop()
        result["omitted_groups"] += 1
    return result


def validate_minimization(review, edition, events, focus):
    if not isinstance(review, dict):
        return ["Minimization review must be an object"]
    groups = focus["groups"]
    decisions = review.get("minimization", [])
    if not isinstance(decisions, list) or len(decisions) != len(groups):
        return ["Minimization review must account for each indexed reduced event exactly once"]
    refs = [group["ref"] for group in groups]
    if any(not isinstance(item, dict) or item.get("ref") not in refs for item in decisions):
        return ["Minimization review contains an unknown reduced-event reference"]
    if len({item["ref"] for item in decisions}) != len(refs):
        return ["Minimization review repeats a reduced-event reference"]
    sources = {event["ref"]: payload_text(source_payload(event)) for event in events}
    errors = []
    for item in decisions:
        prefix = "Minimization " + item["ref"] + ": "
        status = item.get("status")
        if not isinstance(status, str) or status not in {"excluded", "necessary", "needs_fix"}:
            errors.append(prefix + "status must be excluded, necessary or needs_fix")
        if not isinstance(item.get("note"), str) or not item["note"].strip():
            errors.append(prefix + "explain technical necessity or exclusion without guessing removed content")
        quote = item.get("source_quote")
        if not isinstance(quote, str) or not quote.strip() or len(quote) > 500 or quote not in sources[item["ref"]]:
            errors.append(prefix + "source_quote must be a short literal span of the reduced payload")
        authored = item.get("authored")
        if not isinstance(authored, list) or len(authored) > 4:
            errors.append(prefix + "authored must contain up to four exact current-edition locations")
            continue
        if status in ("necessary", "needs_fix") and not authored:
            errors.append(prefix + "retained context or a defect needs an authored location")
        for location in authored:
            if not isinstance(location, dict):
                errors.append(prefix + "invalid authored location")
                continue
            try:
                value = pointer_value(edition, location.get("path"))
            except ValueError:
                value = None
            quote = location.get("quote")
            if not isinstance(quote, str) or len(quote) > 500 or not field_has_quote(value, quote):
                errors.append(prefix + "authored quote must exist at its current-edition JSON pointer")
        issues = review.get("issues", [])
        if status == "needs_fix" and (not isinstance(issues, list) or not any(isinstance(issue, dict) and issue.get("category") == "data_minimization" for issue in issues)):
            errors.append(prefix + "needs_fix requires a grounded data_minimization issue")
    return errors
