import ast
import json
import re
import warnings

from .review_crosswalk import cited_claims
from .source_excerpt import excerpt_segments, source_payload
from .story_grounding import text_values


SCHEMA = "assertion-scope/v1"
MAX_PAYLOAD_CHARS = 40000
MAX_INDEX_CHARS = 24000
MAX_ASSERTIONS = 24
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
            if len(re.findall(r"^\s*\d+→", text, re.MULTILINE)) >= 2:
                normalized = re.sub(r"^[ \t]*\d+→", "", text, flags=re.MULTILINE)
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", SyntaxWarning)
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
                       "predicates and other languages remain in the full source, not silently proved. Verify all authored counterparts."}
    for item in candidates:
        matching = [claim for claim in claims if item["ref"] in claim["refs"]]
        surfaces = {}
        for claim in matching:
            parts = claim["path"].split("/")
            depth = 4 if parts[1:3] == ["article", "agent_detail"] else 3
            surfaces.setdefault("/".join(parts[:depth]), []).append(claim)
        selected = []
        while any(surfaces.values()) and len(selected) < 4:
            for pending in surfaces.values():
                if pending and len(selected) < 4:
                    claim = pending.pop(0)
                    selected.append({"path": claim["path"], "segments": excerpt_segments(claim["quote"], 900)})
        item.update(claims=selected, omitted_claims=len(matching) - len(selected))
        result["assertions"].append(item)
        if len(json.dumps(result, ensure_ascii=False)) > MAX_INDEX_CHARS:
            result["assertions"].pop()
            result["omitted_assertions"] += 1
    while len(json.dumps(result, ensure_ascii=False)) > MAX_INDEX_CHARS and result["assertions"]:
        result["assertions"].pop()
        result["omitted_assertions"] += 1
    return result
