import copy
import json
import re

from .prompts import SCHEMA


class MissingAnchorError(ValueError):
    pass


def missing_anchor_diagnostic(spec, parts, old, allowed_roots):
    limits = {"nodes": 512, "depth": 32, "string_characters": 20000, "matches": 4, "path_characters": 240, "excerpt_characters": 160}

    def excerpt(value, start=0):
        return {"text": value[start:start + limits["excerpt_characters"]], "start": start,
                "characters": len(value), "truncated": start > 0 or len(value) > start + limits["excerpt_characters"]}

    try:
        target = spec
        for part in parts:
            if isinstance(target, list):
                if not re.fullmatch(r"0|[1-9][0-9]*", part):
                    raise ValueError("Invalid original array index")
                target = target[int(part)]
            else:
                target = target[part]
        target_view = excerpt(target) if isinstance(target, str) else {"state": "not_a_string_before_batch"}
    except (KeyError, IndexError, TypeError, ValueError):
        target_view = {"state": "missing_before_batch"}
    diagnostic = {"basis": "original_before_batch", "target": target_view, "exact_matches": [],
                  "search_complete": True, "matches_omitted": False, "nodes_visited": 0, "limits": limits}

    def display_child(path, truncated, key):
        if truncated:
            return path, True
        remaining = max(0, limits["path_characters"] - len(path) - 1)
        raw = str(key)
        escaped = raw[:remaining + 1].replace("~", "~0").replace("/", "~1")
        child = path + "/" + escaped[:remaining]
        return child[:limits["path_characters"]], len(path) + 1 > limits["path_characters"] or len(raw) > remaining or len(escaped) > remaining

    def visit(value, path, depth, path_truncated=False):
        if diagnostic["nodes_visited"] >= limits["nodes"]:
            diagnostic["search_complete"] = False
            return
        diagnostic["nodes_visited"] += 1
        if isinstance(value, str):
            bounded = value[:limits["string_characters"]]
            if len(bounded) < len(value):
                diagnostic["search_complete"] = False
            position = bounded.find(old)
            if position >= 0:
                if len(diagnostic["exact_matches"]) >= limits["matches"]:
                    diagnostic["matches_omitted"] = True
                else:
                    diagnostic["exact_matches"].append({"path": path,
                        "path_truncated": path_truncated, "anchor_start": position,
                        "excerpt": excerpt(value, max(0, position - 40))})
        elif isinstance(value, (dict, list)):
            if depth >= limits["depth"]:
                diagnostic["search_complete"] = False
                return
            children = value.items() if isinstance(value, dict) else enumerate(value)
            for key, child in children:
                if diagnostic["nodes_visited"] >= limits["nodes"]:
                    diagnostic["search_complete"] = False
                    return
                child_path, truncated = display_child(path, path_truncated, key)
                visit(child, child_path, depth + 1, truncated)

    if isinstance(spec, dict):
        for root, value in spec.items():
            if root in allowed_roots:
                if diagnostic["nodes_visited"] >= limits["nodes"]:
                    diagnostic["search_complete"] = False
                    break
                root_path, truncated = display_child("", False, root)
                visit(value, root_path, 0, truncated)
    return json.dumps(diagnostic, ensure_ascii=False)


def apply_spec_patches(spec, response):
    return apply_data_patches(spec, response, SCHEMA)


def replace_anchored_text(current, patch):
    old, value = patch.get("old"), patch.get("value")
    if not isinstance(current, str) or not isinstance(old, str) or not old or not isinstance(value, str):
        raise ValueError("replace_text needs an existing string target, nonempty literal old text and string value.")
    position = current.find(old)
    if position < 0:
        raise MissingAnchorError("replace_text old text does not occur at this exact target; recheck the zero-based path and original text.")
    if current.find(old, position + 1) >= 0:
        raise ValueError("replace_text old text is ambiguous at this target; include more literal surrounding context.")
    return current[:position] + value + current[position + len(old):]


def apply_data_patches(spec, response, allowed_roots):
    patches = response.get("patches") if isinstance(response, dict) else None
    if not isinstance(patches, list) or not patches or len(patches) > 150:
        raise ValueError("Expected between 1 and 150 bounded spec patches.")
    result = copy.deepcopy(spec)
    for patch_number, patch in enumerate(patches, 1):
        if not isinstance(patch, dict) or patch.get("op") not in {"add", "replace", "remove", "replace_text"}:
            raise ValueError("Only add, replace, remove and replace_text data operations are allowed.")
        path = patch.get("path")
        if not isinstance(path, str) or not path.startswith("/") or re.search(r"~(?![01])", path):
            raise ValueError("Invalid JSON pointer in spec patch.")
        parts = [part.replace("~1", "/").replace("~0", "~") for part in path[1:].split("/")]
        if parts[0] not in allowed_roots:
            raise ValueError("Patch must target a canonical spec field.")
        operation = patch["op"]
        if operation != "remove" and "value" not in patch:
            raise ValueError("Patch needs a value.")
        parent = result
        try:
            for part in parts[:-1]:
                if isinstance(parent, list):
                    if not re.fullmatch(r"0|[1-9][0-9]*", part):
                        raise ValueError("Invalid array index in spec patch.")
                    parent = parent[int(part)]
                elif isinstance(parent, dict):
                    parent = parent[part]
                else:
                    raise ValueError("Spec patch traverses a scalar.")
            key = parts[-1]
            if isinstance(parent, list):
                if key == "-" and operation == "add":
                    index = len(parent)
                elif re.fullmatch(r"0|[1-9][0-9]*", key):
                    index = int(key)
                else:
                    raise ValueError("Invalid array index in spec patch.")
                if index > len(parent) or (index == len(parent) and operation != "add"):
                    raise ValueError("Spec patch array index is out of bounds.")
                if operation == "add":
                    parent.insert(index, copy.deepcopy(patch["value"]))
                elif operation == "replace":
                    parent[index] = copy.deepcopy(patch["value"])
                elif operation == "replace_text":
                    parent[index] = replace_anchored_text(parent[index], patch)
                else:
                    parent.pop(index)
            elif isinstance(parent, dict):
                if operation != "add" and key not in parent:
                    advice = " Use add to create the missing field under its existing parent." if operation == "replace" else " Remove only an existing field."
                    raise ValueError("Spec patch targets a missing field." + advice)
                if operation == "remove":
                    del parent[key]
                elif operation == "replace_text":
                    parent[key] = replace_anchored_text(parent[key], patch)
                else:
                    parent[key] = copy.deepcopy(patch["value"])
            else:
                raise ValueError("Spec patch targets a scalar parent.")
        except MissingAnchorError as error:
            diagnostic = missing_anchor_diagnostic(spec, parts, patch["old"], allowed_roots)
            raise ValueError(f"Patch {patch_number} ({operation} at {path!r}): {error} "
                             "No changes applied. Navigation only, not relocation or semantic approval; inspect CURRENT_EDITION before submitting a new exact patch. "
                             f"Anchor diagnostic: {diagnostic}") from error
        except (KeyError, IndexError, TypeError) as error:
            raise ValueError(f"Patch {patch_number} ({operation} at {path!r}): parent path does not exist; add the missing container before its children.") from error
        except ValueError as error:
            raise ValueError(f"Patch {patch_number} ({operation} at {path!r}): {error}") from error
    return result
