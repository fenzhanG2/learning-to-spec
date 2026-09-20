import copy
import difflib
import json
import re


def text_values(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for nested in value.values():
            yield from text_values(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from text_values(nested)


def field_has_quote(field, quote):
    if not isinstance(quote, str) or not quote:
        return False
    if isinstance(field, (list, dict)) and not field:
        return quote == json.dumps(field)
    return any(quote in text for text in text_values(field))


def quote_diagnostic(field, quote):
    candidates = [line for text in text_values(field) for line in text.splitlines() if line.strip()]
    nearby = difflib.get_close_matches(str(quote), candidates, n=1, cutoff=0.05)
    hint = (nearby[0] if nearby else next(text_values(field), json.dumps(field, ensure_ascii=False)))[:1200]
    return (f"quote must be exact existing text at this location; rejected quote={quote!r}. "
            f"Actual source line (navigation hint, not an automatic replacement): {hint!r}. "
            "Preserve case, punctuation, Markdown **/backticks/escapes; use a short continuous span. "
            "An empty array/object may be quoted exactly as []/{} at its actual path when identifying an omission.")


def pointer_value(document, pointer):
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise ValueError("A finding must point into the actual edition")
    value = document
    try:
        for part in pointer[1:].split("/"):
            key = part.replace("~1", "/").replace("~0", "~")
            if isinstance(value, list):
                if not key.isdecimal():
                    raise ValueError("Invalid array index")
                value = value[int(key)]
            else:
                value = value[key]
    except (KeyError, IndexError, ValueError, TypeError):
        raise ValueError("Finding path does not exist in the actual edition") from None
    return value


def complete_reference_pairs(document, events):
    pairs, references = {}, {}
    requests, results = {}, {}
    for event in events:
        call_id = event.get("tool_call_id")
        if call_id and event.get("type") in {"tool.execution_start", "tool.execution_complete"}:
            pairs.setdefault(call_id, []).append(event["ref"])
            references[event["ref"]] = call_id
            counts = requests if event["type"] == "tool.execution_start" else results
            counts[call_id] = counts.get(call_id, 0) + 1
    pairs = {call_id: refs for call_id, refs in pairs.items() if requests.get(call_id) == 1 and results.get(call_id, 0) >= 1}
    result = copy.deepcopy(document)
    changes = []

    def visit(value, path=""):
        if isinstance(value, dict):
            for key, nested in value.items():
                location = path + "/" + key.replace("~", "~0").replace("/", "~1")
                if key in {"refs", "tool_refs"} and isinstance(nested, list) and all(isinstance(ref, str) for ref in nested):
                    supplied = list(nested)
                    for ref in supplied:
                        for linked in pairs.get(references.get(ref), []):
                            if linked not in nested:
                                nested.append(linked)
                    if supplied != nested:
                        changes.append({"path": location, "supplied": supplied, "added": [ref for ref in nested if ref not in supplied]})
                else:
                    visit(nested, location)
        elif isinstance(value, list):
            for index, nested in enumerate(value):
                visit(nested, path + "/" + str(index))
    visit(result)
    return result, changes


def source_values(event, origin):
    if origin == "human":
        return [event.get("human_input") or ""]
    if origin == "tool" and event.get("type", "").startswith("tool."):
        payload = {key: event.get(key) for key in ("arguments", "result", "error")}
        return [*text_values(payload), json.dumps(payload, ensure_ascii=False)]
    if origin == "assistant" and event.get("type") == "assistant.message":
        return [event.get("text") or ""]
    if origin == "context" and event.get("type") not in {"user.message", "assistant.message", "tool.execution_start", "tool.execution_complete"}:
        return [event.get("text") or ""]
    if origin == "metadata":
        payload = {key: event.get(key) for key in ("timestamp", "source_turn_id", "import_metadata")}
        return [*text_values(payload), json.dumps(payload, ensure_ascii=False)]
    return []


def quote_basis(event, origin, quote):
    if not isinstance(quote, str) or not quote:
        return None
    values = source_values(event, origin)
    if any(quote in value for value in values):
        return "literal"
    if origin == "tool" and str(event.get("tool", "")).casefold() in {"read", "read_file", "view"}:
        for value in text_values(event.get("result", {})):
            if len(re.findall(r"^\s*\d+→", value, re.MULTILINE)) >= 2:
                plain = re.sub(r"^[ \t]*\d+→", "", value, flags=re.MULTILINE)
                if quote in plain:
                    return "read_display_line_prefixes_removed"
    return None


def string_locations(value, path=""):
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key, nested in value.items():
            yield from string_locations(nested, path + "/" + key.replace("~", "~0").replace("/", "~1"))
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            yield from string_locations(nested, path + "/" + str(index))


def resolve_review_locations(review, edition, events, contract=None):
    resolved = copy.deepcopy(review)
    changes = []
    evidence = {event["ref"]: event for event in events}
    for index, issue in enumerate(resolved.get("issues", [])):
        contract_quote = issue.get("contract_quote")
        if (contract and issue.get("kind") == "contract" and isinstance(contract_quote, str) and contract_quote
                and contract_quote not in contract and "`" not in contract_quote):
            quotation_marks = str.maketrans({"\u201c": '"', "\u201d": '"'})
            normalized = contract.translate(quotation_marks)
            needle = contract_quote.translate(quotation_marks)
            start = normalized.find(needle)
            if start >= 0 and normalized.find(needle, start + 1) < 0:
                literal = contract[start:start + len(contract_quote)]
                changes.append({"issue": index, "kind": "contract_quotation_marks", "from": contract_quote, "to": literal})
                issue["contract_quote"] = literal
        quote = issue.get("quote")
        try:
            field = pointer_value(edition, issue.get("path"))
        except ValueError:
            field = None
        if isinstance(quote, str) and quote and not field_has_quote(field, quote):
            matches = [path for path, text in string_locations(edition) if quote in text]
            if len(matches) == 1:
                changes.append({"issue": index, "kind": "candidate_path", "from": issue.get("path"), "to": matches[0]})
                issue["path"] = matches[0]
        anchors = issue.get("evidence", [])
        if not isinstance(anchors, list):
            continue
        for anchor in anchors:
            if not isinstance(anchor, dict) or not isinstance(anchor.get("ref"), str):
                continue
            old_ref, origin, quote = anchor.get("ref"), anchor.get("origin"), anchor.get("quote")
            basis = quote_basis(evidence.get(old_ref, {}), origin, quote)
            if not basis:
                matches = [(event["ref"], quote_basis(event, origin, quote)) for event in events]
                matches = [(ref, matched) for ref, matched in matches if matched]
                if len(matches) == 1:
                    new_ref, basis = matches[0]
                    changes.append({"issue": index, "kind": "source_reference", "from": old_ref, "to": new_ref, "origin": origin})
                    anchor["ref"] = new_ref
                    if isinstance(issue.get("refs"), list):
                        issue["refs"] = list(dict.fromkeys([new_ref if ref == old_ref else ref for ref in issue["refs"]] + [new_ref]))
            if basis == "read_display_line_prefixes_removed":
                changes.append({"issue": index, "kind": basis, "ref": anchor.get("ref")})
    return resolved, changes


def validate_grounding(review, edition, events, contract):
    errors = []
    evidence = {event["ref"]: event for event in events}
    for index, issue in enumerate(review.get("issues", [])):
        prefix = f"Finding {index}: "
        if issue.get("severity", "major") not in {"critical", "major"}:
            errors.append(prefix + "minor/style preferences belong in suggestions, not blocking issues")
        try:
            field = pointer_value(edition, issue.get("path"))
        except ValueError as error:
            errors.append(prefix + str(error))
            continue
        quote = issue.get("quote")
        if not field_has_quote(field, quote):
            errors.append(prefix + "path=" + str(issue.get("path")) + ": " + quote_diagnostic(field, quote))
        kind = issue.get("kind")
        if kind == "contract" and re.fullmatch(r"/insights/architecture/nodes/\d+/role", issue.get("path", "")) and field in ("入口", "控制", "处理", "存储", "产物", "外部"):
            errors.append(prefix + "role is a valid internal enum localized by the renderer, not untranslated prose; withdraw this language-contract finding, do not translate enums or remove the diagram")
        anchors = issue.get("evidence", [])
        if kind not in {"human_requirement", "source_fact", "contract"} or not isinstance(anchors, list):
            errors.append(prefix + "kind must be human_requirement/source_fact/contract with an evidence array")
            continue
        if kind == "contract":
            rule = issue.get("contract_quote")
            if not isinstance(rule, str) or not rule or rule not in contract:
                errors.append(prefix + "contract_quote must literally exist in the supplied publishing contract; do not invent editorial rules")
        elif not anchors:
            errors.append(prefix + "a factual/intent finding requires source quotations")
        human_support = False
        for anchor in anchors:
            if not isinstance(anchor, dict) or not isinstance(anchor.get("ref"), str) or anchor.get("ref") not in evidence:
                errors.append(prefix + "unknown source quotation reference")
                continue
            event = evidence[anchor["ref"]]
            origin = anchor.get("origin")
            source_quote = anchor.get("quote")
            supported = bool(quote_basis(event, origin, source_quote))
            if not supported:
                errors.append(prefix + f"{anchor['ref']} quotation does not exist for origin={origin}; use a short literal source span with the correct role and reference")
            human_support |= bool(supported and origin == "human")
        if kind == "human_requirement" and not human_support:
            errors.append(prefix + "user intent/choice/authorization needs a literal human-input quotation")
    suggestions = review.get("suggestions", [])
    if not isinstance(suggestions, list) or any(not isinstance(item, dict) or not isinstance(item.get("reason"), str) for item in suggestions):
        errors.append("Editorial suggestions must be a list of reasons, separate from blocking facts/contracts")
    return errors
