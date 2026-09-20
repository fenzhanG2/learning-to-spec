import ast
import json
import warnings

from .review_crosswalk import cited_claims
from .source_excerpt import excerpt_segments, read_display_text, source_payload
from .story_grounding import pointer_value, text_values


SCHEMA = "assertion-scope/v4"
MAX_PAYLOAD_CHARS = 40000
MAX_INDEX_CHARS = 24000
MAX_ASSERTIONS = 24
MAX_CLAIMS = 8
FACT_FIELDS = {"finding", "observed", "observation", "verification_boundary", "precondition", "expected", "done_when", "avoid", "adapt", "reuse_condition", "markdown", "agent_markdown"}
NAVIGATION_FIELDS = {"title", "subtitle", "kind", "label", "purpose", "scope"}
RELATIONS = {
    "endswith": ("suffix predicate", "A suffix check permits an additional prefix; it does not establish whole-value equality.", "prefix_sample", "sample"),
    "startswith": ("prefix predicate", "A prefix check permits an additional suffix; it does not establish whole-value equality.", "sample_suffix", "sample"),
    "membership": ("membership predicate", "Containment does not establish whole-value equality or absence of additional content.", "before_sample_after", "sample"),
    "equality": ("equality predicate", "Equality concerns only the displayed operands, not unrelated fields, code paths or execution of this assertion.", None, None),
}


def relation_of(condition):
    if isinstance(condition, ast.Call) and isinstance(condition.func, ast.Attribute) and len(condition.args) == 1 and not condition.keywords:
        if condition.func.attr in ("endswith", "startswith") and not isinstance(condition.args[0], ast.Starred):
            return condition.func.attr
    if isinstance(condition, ast.Compare) and len(condition.ops) == 1:
        if isinstance(condition.ops[0], ast.In):
            return "membership"
        if isinstance(condition.ops[0], ast.Eq):
            return "equality"
    return None


def assertion_scope(edition, events):
    claims = cited_claims(edition)
    candidates = []
    recognized = 0
    skipped_payloads = 0
    unsupported_assertions = 0
    for event in events:
        if event.get("type") != "tool.execution_complete" or str(event.get("tool", "")).casefold() not in {"read", "read_file", "view"}:
            continue
        seen = set()
        for text in text_values(source_payload(event)):
            if text in seen:
                continue
            seen.add(text)
            if len(text) > MAX_PAYLOAD_CHARS:
                skipped_payloads += 1
                continue
            normalized = text
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", SyntaxWarning)
                    try:
                        tree = ast.parse(text)
                    except SyntaxError:
                        normalized = read_display_text(text)
                        if normalized == text:
                            raise
                        tree = ast.parse(normalized)
            except (SyntaxError, ValueError, RecursionError):
                skipped_payloads += 1
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Assert):
                    continue
                relation = relation_of(node.test)
                quote = ast.get_source_segment(normalized, node)
                if not relation or not quote or len(quote) > 1400:
                    unsupported_assertions += 1
                    continue
                recognized += 1
                label, limitation, observed, expected = RELATIONS[relation]
                item = {"ref": event["ref"], "quote": quote, "line": node.lineno, "relation": label, "tool_success": event.get("success"),
                        "limitation": limitation, "normalization": "read_line_prefixes_removed" if normalized != text else "none"}
                if observed is not None:
                    item["hypothetical_builtin_string_example"] = {"observed": observed, "expected": expected,
                        "predicate_holds": observed.endswith(expected) if relation == "endswith" else observed.startswith(expected) if relation == "startswith" else expected in observed,
                        "equal": observed == expected, "historical_execution": False}
                if len(candidates) < MAX_ASSERTIONS:
                    candidates.append(item)
                elif relation != "equality":
                    replace = next((index for index in range(len(candidates) - 1, -1, -1) if candidates[index]["relation"] == "equality predicate"), None)
                    if replace is not None:
                        candidates[replace] = item
    candidates.sort(key=lambda item: item["relation"] == "equality predicate")
    result = {"schema": SCHEMA, "assertions": [], "recognized_assertions": recognized,
              "omitted_assertions": recognized - len(candidates), "unsupported_assertions": unsupported_assertions, "unparsed_or_large_payloads": skipped_payloads,
              "limit": "Syntax-only index of bounded complete Python payloads recorded by Read/read_file/view. No source is executed. "
                       "Assertions may be conditional or unreachable; source presence is not a passing test. Method names may be "
                       "custom: resolve receiver semantics before using the builtin-string examples. Examples illustrate a logical "
                       "non-implication, never historical inputs, outputs or execution evidence. Negated/compound/unsupported "
                       "predicates and other languages remain in the full source, not silently proved. Claims are unique whole "
                       "authored fields, not isolated citation paragraphs: references may support only part of each field. "
                       "Substantive findings/preconditions precede navigation labels. Verify all authored counterparts."}
    for item in candidates:
        matching = [claim for claim in claims if item["ref"] in claim["refs"]]
        fields = {}
        for claim in matching:
            if claim["path"] not in fields:
                fields[claim["path"]] = {"path": claim["path"], "quote": pointer_value(edition, claim["path"]), "reference_count": len(claim["refs"])}
            else:
                fields[claim["path"]]["reference_count"] = min(fields[claim["path"]]["reference_count"], len(claim["refs"]))
        surfaces = {}
        for claim in fields.values():
            parts = claim["path"].split("/")
            depth = 4 if parts[1:3] == ["article", "agent_detail"] else 3
            surfaces.setdefault("/".join(parts[:depth]), []).append(claim)
        def priority(claim):
            field = claim["path"].rsplit("/", 1)[-1]
            return (0 if field in FACT_FIELDS else 2 if field in NAVIGATION_FIELDS else 1, claim["reference_count"])
        for pending in surfaces.values():
            pending.sort(key=priority)
        selected = []
        while any(surfaces.values()) and len(selected) < MAX_CLAIMS:
            for pending in surfaces.values():
                if pending and len(selected) < MAX_CLAIMS:
                    claim = pending.pop(0)
                    selected.append({"path": claim["path"], "characters": len(claim["quote"]), "truncated": len(claim["quote"]) > 900,
                                     "segments": excerpt_segments(claim["quote"], 900)})
        item.update(claims=selected, matching_fields=len(fields), matching_citation_parts=len(matching), omitted_claims=len(fields) - len(selected))
        result["assertions"].append(item)
        if len(json.dumps(result, ensure_ascii=False)) > MAX_INDEX_CHARS:
            result["assertions"].pop()
            result["omitted_assertions"] += 1
    while len(json.dumps(result, ensure_ascii=False)) > MAX_INDEX_CHARS and result["assertions"]:
        result["assertions"].pop()
        result["omitted_assertions"] += 1
    return result
