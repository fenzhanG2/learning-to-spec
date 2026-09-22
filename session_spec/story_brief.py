import json
import re
from pathlib import Path

from .patching import apply_data_patches
from .storage import PROMPTS, digest, write_json


SCHEMA = "story-brief/v1"
FIELDS = ("background", "problem", "goals", "approach", "non_goals", "scope", "constraints", "status")
MAX_TEXT_CHARS = 300
MAX_BRIEF_CHARS = 1400
CONSTRAINT_KINDS = ("requirement", "environment")


def validate_brief(value, events):
    if not isinstance(value, dict):
        return ["/brief: expected an opening brief object with schema story-brief/v1"]
    errors = []
    evidence = {event["ref"]: event for event in events}
    if value.get("schema") != SCHEMA or set(value) != {"schema", *FIELDS}:
        errors.append("/brief: expected schema story-brief/v1 and exactly keys " + json.dumps(["schema", *FIELDS]) + "; no extra keys")
    visible = []

    def statement(item, location, human=False, exclusion=False, constraint=False):
        pointer = "/brief/" + location
        expected = {"text", "refs"} | ({"quote"} if exclusion else set()) | ({"kind"} if constraint else set())
        shape = "expected an object with exactly keys " + json.dumps(sorted(expected)) + "; no extra keys"
        if not isinstance(item, dict):
            errors.append(pointer + ": " + shape)
            return
        if set(item) != expected:
            errors.append(pointer + ": " + shape)
        content = item.get("text")
        if isinstance(content, str):
            visible.append(content)
        if not isinstance(content, str) or not content.strip() or len(content) > MAX_TEXT_CHARS:
            errors.append(pointer + f"/text: expected nonempty text; limit={MAX_TEXT_CHARS} characters, actual=" + str(len(content) if isinstance(content, str) else None))
        elif re.search(r"\bE\d{6}\b|<[^>]+>|\]\([^)]*\)", content):
            errors.append(pointer + "/text: visible brief must not contain evidence IDs, markup or links; keep evidence in refs")
        references = item.get("refs")
        if not isinstance(references, list) or not references or any(not isinstance(ref, str) or ref not in evidence for ref in references):
            errors.append(pointer + "/refs: expected a nonempty array of existing source-reference strings")
            return
        inputs = [evidence[ref]["human_input"] for ref in references if isinstance(evidence[ref].get("human_input"), str) and evidence[ref]["human_input"]]
        if human and not inputs:
            errors.append(pointer + "/refs: goals, non_goals and requirement constraints must cite a source event with nonempty human_input; assistant/tool reports are not user intent")
        if exclusion:
            quote = item.get("quote")
            if not isinstance(quote, str) or not quote.strip() or not any(quote in source for source in inputs):
                errors.append(pointer + "/quote: expected a nonempty exact substring of human_input in one of this item's cited events; do not invent exclusions")

    for field in ("background", "problem", "approach", "status"):
        statement(value.get(field), field)
    for field, minimum, maximum in (("goals", 1, 3), ("non_goals", 0, 3), ("scope", 0, 3), ("constraints", 0, 4)):
        items = value.get(field)
        if not isinstance(items, list) or not minimum <= len(items) <= maximum:
            errors.append(f"/brief/{field}: expected an array with {minimum}–{maximum} items")
            continue
        for index, item in enumerate(items):
            requirement = field == "constraints" and isinstance(item, dict) and item.get("kind") == "requirement"
            if field == "constraints" and (not isinstance(item, dict) or item.get("kind") not in CONSTRAINT_KINDS):
                errors.append(f"/brief/constraints/{index}/kind: expected one of " + json.dumps(CONSTRAINT_KINDS)
                              + "; requirement needs cited human input; environment describes recorded context or limits. Choose from source provenance, not to bypass validation")
            statement(item, f"{field}/{index}", human=field in ("goals", "non_goals") or requirement,
                      exclusion=field == "non_goals", constraint=field == "constraints")
    if sum(map(len, visible)) > MAX_BRIEF_CHARS:
        errors.append(f"/brief: visible text totals {sum(map(len, visible))} characters, above the hard {MAX_BRIEF_CHARS}-character bound. Compress /brief toward 400–650; changing unused /article/opening cannot repair this.")
    return errors


def prepare_brief(directory, backend, model=None):
    directory = Path(directory)
    article_bytes = (directory / "article.json").read_bytes()
    input_bytes = (directory / "input.json").read_bytes()
    source_identity = {"article_sha256": digest(article_bytes), "input_sha256": digest(input_bytes), "schema": SCHEMA}
    for candidate_name, receipt_name, hash_field in (
        ("brief-candidate.json", "brief-attempt.json", "candidate_sha256"),
        ("brief-draft.json", "brief-draft-receipt.json", "output_sha256"),
        ("brief.json", "brief-receipt.json", "output_sha256"),
    ):
        candidate_path, receipt_path = directory / candidate_name, directory / receipt_name
        if not candidate_path.is_file() or not receipt_path.is_file():
            continue
        receipt = json.loads(receipt_path.read_bytes())
        if all(receipt.get("identity", {}).get(key) == value for key, value in source_identity.items()) and receipt.get(hash_field) == digest(candidate_path.read_bytes()):
            candidate = json.loads(candidate_path.read_bytes())
            if not validate_brief(candidate, json.loads(input_bytes)):
                print("[brief draft reused] requires whole-document review", flush=True)
                return candidate, receipt.get("failures", {}).get(digest(candidate_path.read_bytes()), [])
    template = (PROMPTS / "story-brief.md").read_text(encoding="utf-8")
    context = "\n\nHISTORICAL_EVENTS\n" + input_bytes.decode("utf-8")
    context += "\n\nARTICLE_FOR_CONTINUITY_NOT_AUTHORITY\n" + article_bytes.decode("utf-8")
    candidate = backend.generate(template + context, "story-brief-draft")
    output = directory / "brief-draft.json"
    write_json(output, candidate)
    write_json(directory / "brief-draft-receipt.json", {
        "identity": source_identity, "status": "unreviewed", "model": model or "copilot-default",
        "prompt_sha256": digest(template.encode()), "output_sha256": digest(output.read_bytes()),
    })
    return candidate, []


def generate_brief(directory, backend, model=None, max_repairs=2):
    directory = Path(directory)
    article_bytes = (directory / "article.json").read_bytes()
    event_bytes = (directory / "input.json").read_bytes()
    article, events = json.loads(article_bytes), json.loads(event_bytes)
    template = (PROMPTS / "story-brief.md").read_text(encoding="utf-8")
    review_template = (PROMPTS / "story-brief-review.md").read_text(encoding="utf-8")
    identity = {"schema": SCHEMA, "article_sha256": digest(article_bytes), "input_sha256": digest(event_bytes),
                "prompt_sha256": digest(template.encode()), "review_prompt_sha256": digest(review_template.encode()),
                "model": model or "copilot-default"}
    output, receipt_path = directory / "brief.json", directory / "brief-receipt.json"
    if output.is_file() and receipt_path.is_file():
        cached = json.loads(receipt_path.read_bytes())
        candidate = json.loads(output.read_bytes())
        if (cached.get("identity") == identity and cached.get("status") == "completed"
                and cached.get("output_sha256") == digest(output.read_bytes()) and not validate_brief(candidate, events)):
            print("[brief cached] opening context", flush=True)
            return candidate
    attempt_path, candidate_path = directory / "brief-attempt.json", directory / "brief-candidate.json"
    receipt = {"identity": identity, "status": "running", "failures": {}, "attempts": []}
    candidate = None
    if attempt_path.is_file() and candidate_path.is_file():
        previous = json.loads(attempt_path.read_bytes())
        previous_identity = previous.get("identity", {})
        reusable = all(previous_identity.get(key) == identity[key] for key in ("schema", "article_sha256", "input_sha256"))
        if reusable and previous.get("candidate_sha256") == digest(candidate_path.read_bytes()):
            candidate = json.loads(candidate_path.read_bytes())
            receipt["failures"] = previous.get("failures", {})
    context = "\n\nHISTORICAL_EVENTS\n" + json.dumps(events, ensure_ascii=False)
    context += "\n\nARTICLE_FOR_CONTINUITY_NOT_AUTHORITY\n" + json.dumps(article, ensure_ascii=False)
    feedback = ""
    first_call = len(backend.calls)
    print("[brief start] background, problem, goals, approach and boundaries", flush=True)
    try:
        for attempt in range(max_repairs + 1):
            if candidate is None:
                candidate = backend.generate(template + context, "story-brief")
            elif attempt > 0:
                patch = backend.generate(template + context + feedback +
                                         "\n只返回 JSON 数据补丁 {\"patches\":[{\"op\":\"replace\",\"path\":\"/problem/text\",\"value\":\"修复文本\"}]}。仅允许 add/replace/remove；仅可修改概览八个内容字段，不改 schema，不改故事或 Agent。复核建议不是事实，须核对原始事件。", "story-brief-patch")
                write_json(directory / f"brief-patch-{attempt}.json", patch)
                try:
                    candidate = apply_data_patches(candidate, patch, FIELDS)
                except ValueError as error:
                    feedback += "\n补丁未应用：" + str(error)
                    receipt["attempts"].append({"attempt": attempt, "patch_error": str(error)})
                    continue
            write_json(candidate_path, candidate)
            fingerprint = digest(candidate_path.read_bytes())
            receipt["candidate_sha256"] = fingerprint
            write_json(attempt_path, receipt)
            errors = validate_brief(candidate, events) or receipt["failures"].get(fingerprint, [])
            if not errors:
                review = backend.generate(review_template + "\n\nWRITING_CONTRACT\n" + template + context +
                                          "\n\nBRIEF_TO_REVIEW\n" + json.dumps(candidate, ensure_ascii=False) +
                                          "\n只返回 issues/summary 复核 JSON，不返回概览或文章。", "story-brief-review")
                write_json(directory / f"brief-review-{attempt}.json", review)
                errors = review.get("issues")
                if not isinstance(errors, list) or any(not isinstance(issue, dict) or not issue.get("reason") for issue in errors):
                    raise ValueError("Invalid opening brief review; publication refused")
            receipt["attempts"].append({"attempt": attempt, "issues": errors})
            if not errors:
                write_json(output, candidate)
                receipt.update(status="completed", output_sha256=digest(output.read_bytes()))
                receipt["calls"] = backend.calls[first_call:]
                write_json(receipt_path, receipt)
                print("[brief accepted] opening context", flush=True)
                return candidate
            receipt["failures"][fingerprint] = errors
            write_json(attempt_path, receipt)
            feedback = "\n\n仅修复下面候选的实质问题，保留正确字段：\n" + json.dumps({"candidate": candidate, "issues": errors}, ensure_ascii=False)
            print(f"[brief repair] {len(errors)} issues", flush=True)
        raise ValueError("Opening brief failed bounded review; no new document published")
    except Exception as error:
        receipt.update(status="failed", error=str(error))
        raise
    finally:
        receipt["calls"] = backend.calls[first_call:]
        write_json(attempt_path, receipt)
