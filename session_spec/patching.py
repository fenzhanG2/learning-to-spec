import copy
import re

from .prompts import SCHEMA


def apply_spec_patches(spec, response):
    return apply_data_patches(spec, response, SCHEMA)


def replace_anchored_text(current, patch):
    old, value = patch.get("old"), patch.get("value")
    if not isinstance(current, str) or not isinstance(old, str) or not old or not isinstance(value, str):
        raise ValueError("replace_text needs an existing string target, nonempty literal old text and string value.")
    position = current.find(old)
    if position < 0:
        raise ValueError("replace_text old text does not occur at this exact target; recheck the zero-based path and original text.")
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
        except (KeyError, IndexError, TypeError) as error:
            raise ValueError(f"Patch {patch_number} ({operation} at {path!r}): parent path does not exist; add the missing container before its children.") from error
        except ValueError as error:
            raise ValueError(f"Patch {patch_number} ({operation} at {path!r}): {error}") from error
    return result
