import argparse
import json
import re
from pathlib import Path

from .backend import CopilotBackend
from .patching import apply_data_patches
from .storage import PROMPTS, digest, write_json


SCHEMA = "story-insights/v1"


def validate_insights(value, article, events):
    errors = []
    chapters = [chapter["id"] for chapter in article["chapters"]]
    evidence = {event["ref"]: event for event in events}

    def require(condition, message):
        if not condition:
            errors.append(message)

    def prose(item, field, maximum=2000, location="/insights"):
        content = item.get(field)
        target = location if field == "paragraph" else location + "/" + field
        require(isinstance(content, str) and bool(content.strip()), f"Missing text: {field} at {target}")
        if isinstance(content, str):
            require(len(content) <= maximum, f"Text too long: {field} at {target}; limit={maximum}, actual={len(content)}")
            require(not re.search(r"\bE\d{6}\b|<[^>]+>", content), f"Visible text contains evidence IDs or markup: {field} at {target}")

    def refs(item, location, needs_result=False):
        references = item.get("refs")
        if not isinstance(references, list) or not references or any(not isinstance(ref, str) for ref in references):
            errors.append(f"Missing references: {location}")
            return
        require(all(ref in evidence for ref in references), f"Unknown reference: {location}")
        if needs_result:
            require(any(
                evidence.get(ref, {}).get("type") == "tool.execution_complete"
                and evidence[ref].get("result") and evidence[ref].get("success") is not False
                and not evidence[ref].get("error")
                for ref in references
            ), f"No completion/readback evidence: {location}")

    if not isinstance(value, dict):
        return ["Expected an insights object"]
    require(value.get("schema") == SCHEMA, "Unsupported insights schema")
    architecture = value.get("architecture")
    closing = value.get("closing")
    if not isinstance(architecture, dict) or not isinstance(closing, dict):
        return errors + ["Architecture and closing objects are required"]
    prose(architecture, "reason", location="/insights/architecture")
    if architecture.get("decision") == "omit":
        require(set(architecture) == {"decision", "reason"}, "Omitted architecture must not contain a placeholder graph")
    elif architecture.get("decision") == "include":
        require(architecture.get("after_chapter") in chapters, "Architecture chapter does not exist")
        require(architecture.get("kind") in ("final_artifact", "implementation_process"), "Invalid architecture time slice")
        for field in ("title", "scope", "evidence_summary", "limits"):
            prose(architecture, field, location="/insights/architecture")
        nodes, edges = architecture.get("nodes"), architecture.get("edges")
        if not isinstance(nodes, list) or not isinstance(edges, list):
            return errors + ["Graph nodes and edges must be arrays"]
        require(2 <= len(nodes) <= 9 and 1 <= len(edges) <= 12, "Graph must have 2–9 nodes and 1–12 edges")
        ids = []
        for node_index, node in enumerate(nodes):
            if not isinstance(node, dict):
                errors.append("Invalid graph node")
                continue
            identifier = node.get("id")
            require(isinstance(identifier, str) and bool(re.fullmatch(r"[a-z][a-z0-9-]*", identifier)), "Invalid graph node id")
            if isinstance(identifier, str):
                ids.append(identifier)
            require(node.get("role") in ("入口", "控制", "处理", "存储", "产物", "外部"), "Invalid graph node role")
            prose(node, "title", 48, f"/insights/architecture/nodes/{node_index}")
            prose(node, "detail", 100, f"/insights/architecture/nodes/{node_index}")
            refs(node, f"node {identifier}", needs_result=True)
        require(len(set(ids)) == len(ids), "Duplicate graph node id")
        neighbors = {identifier: set() for identifier in ids}
        relations = set()
        for index, edge in enumerate(edges):
            if not isinstance(edge, dict):
                errors.append("Invalid graph edge")
                continue
            origin, target = edge.get("from"), edge.get("to")
            valid = isinstance(origin, str) and isinstance(target, str) and origin in neighbors and target in neighbors
            require(valid, "Dangling graph edge")
            require(edge.get("kind") in ("flow", "feedback"), "Invalid edge kind")
            require(edge.get("basis") in ("implementation", "observation"), "Unknown or proposed edges cannot be drawn as implemented")
            prose(edge, "label", 80, f"/insights/architecture/edges/{index}")
            refs(edge, f"edge {index}", needs_result=True)
            if valid:
                neighbors[origin].add(target)
                neighbors[target].add(origin)
                relation = (origin, target, str(edge.get("kind")))
                require(relation not in relations, "Duplicate graph edge")
                relations.add(relation)
        require(all(neighbors[identifier] - {identifier} for identifier in ids), "Architecture cannot contain isolated concept cards")
    else:
        errors.append("Architecture decision must be include or omit")
    for field in ("title", "principle", "applicability", "non_claim"):
        prose(closing, field, location="/insights/closing")
    paragraphs = closing.get("paragraphs")
    require(isinstance(paragraphs, list) and 1 <= len(paragraphs) <= 3, "Closing needs 1–3 natural paragraphs")
    if isinstance(paragraphs, list):
        for index, paragraph in enumerate(paragraphs):
            prose({"paragraph": paragraph}, "paragraph", location=f"/insights/closing/paragraphs/{index}")
    anchors = closing.get("anchors")
    require(isinstance(anchors, list) and 1 <= len(anchors) <= 3, "Closing needs 1–3 concrete turning-point anchors")
    if isinstance(anchors, list):
        for index, anchor in enumerate(anchors):
            if not isinstance(anchor, dict):
                errors.append("Invalid closing anchor")
                continue
            require(anchor.get("chapter") in chapters, "Closing anchor chapter does not exist")
            prose(anchor, "turning_point", location=f"/insights/closing/anchors/{index}")
            refs(anchor, "closing anchor")
    return errors


def generate_insights(directory, model=None, gh_host=None, backend=None, max_repairs=2):
    directory = Path(directory)
    article_bytes = (directory / "article.json").read_bytes()
    input_bytes = (directory / "input.json").read_bytes()
    article, events = json.loads(article_bytes), json.loads(input_bytes)
    template = (PROMPTS / "story-insights.md").read_text(encoding="utf-8")
    review_template = (PROMPTS / "story-insights-review.md").read_text(encoding="utf-8")
    identity = {
        "article_sha256": digest(article_bytes), "input_sha256": digest(input_bytes),
        "prompt_sha256": digest(template.encode()), "review_prompt_sha256": digest(review_template.encode()),
        "model": model or "copilot-default", "schema": SCHEMA,
    }
    output, receipt_path = directory / "insights.json", directory / "insights-receipt.json"
    if output.is_file() and receipt_path.is_file():
        cached = json.loads(receipt_path.read_text(encoding="utf-8"))
        candidate = json.loads(output.read_bytes())
        if (cached.get("identity") == identity and cached.get("status") == "completed"
                and cached.get("output_sha256") == digest(output.read_bytes())
                and not validate_insights(candidate, article, events)):
            print(f"[insights cached] {directory.name}", flush=True)
            return candidate
    client = backend or CopilotBackend(model=model, gh_host=gh_host, timeout=900, max_calls=2 * (max_repairs + 1))
    context = "\n\nHISTORICAL_ARTICLE\n" + json.dumps(article, ensure_ascii=False)
    context += "\n\nHISTORICAL_EVENTS\n" + json.dumps(events, ensure_ascii=False)
    attempt_path = directory / "insights-attempt-receipt.json"
    previous_candidate = None
    if attempt_path.is_file():
        previous_attempt = json.loads(attempt_path.read_text(encoding="utf-8"))
        previous_identity = previous_attempt.get("identity", {})
        candidates = sorted(directory.glob("insights-candidate-*.json"), key=lambda filename: filename.stat().st_mtime_ns)
        if candidates and previous_identity.get("input_sha256") == identity["input_sha256"]:
            previous_candidate = json.loads(candidates[-1].read_text(encoding="utf-8"))
    receipt = {"identity": identity, "status": "running", "attempts": [], "calls": []}
    write_json(attempt_path, receipt)
    feedback = ""
    candidate = previous_candidate
    print(f"[insights start] {directory.name}", flush=True)
    try:
        for attempt in range(max_repairs + 1):
            prompt = template + context + feedback
            (directory / f"insights-prompt-{attempt}.md").write_text(prompt, encoding="utf-8")
            if attempt == 0 and candidate is None:
                candidate = client.generate(prompt, "story-insights-" + directory.name)
            elif attempt > 0:
                repair_prompt = prompt + "\n\n只返回受限 JSON 数据补丁：{\"patches\":[{\"op\":\"replace\",\"path\":\"/architecture/edges/0/label\",\"value\":\"更准确的标签\"}]}。允许 add/replace/remove，仅可修改 architecture 和 closing，禁止代码、外部路径、schema 修改或整篇重写。选择最小的事实修复；已有正确引用不得因新增代码引用而丢弃，每个节点/边仍须引用实际完成或读回事件。建议不是事实，必须核对原事件。"
                patch = client.generate(repair_prompt, "story-insights-patch-" + directory.name)
                write_json(directory / f"insights-patch-{attempt}.json", patch)
                try:
                    candidate = apply_data_patches(candidate, patch, ("architecture", "closing"))
                except ValueError as error:
                    feedback += "\n上次补丁无效，没有应用任何修改：" + str(error)
                    receipt["attempts"].append({"attempt": attempt, "patch_error": str(error)})
                    continue
            write_json(directory / f"insights-candidate-{attempt}.json", candidate)
            errors = validate_insights(candidate, article, events)
            result = {"attempt": attempt, "structural_errors": errors}
            if not errors:
                review_prompt = review_template + context + "\n\nCANDIDATE\n" + json.dumps(candidate, ensure_ascii=False)
                review = client.generate(review_prompt, "story-insights-review-" + directory.name)
                write_json(directory / f"insights-review-{attempt}.json", review)
                issues = review.get("issues")
                if not isinstance(issues, list) or any(not isinstance(issue, dict) or not issue.get("reason") for issue in issues):
                    raise ValueError("Invalid semantic review; refusing to publish")
                result["review"] = review
                errors = issues
            receipt["attempts"].append(result)
            if not errors:
                write_json(output, candidate)
                receipt.update(status="completed", output_sha256=digest(output.read_bytes()))
                receipt["calls"] = client.calls
                write_json(receipt_path, receipt)
                print(f"[insights accepted] {directory.name}: {candidate['architecture']['decision']}", flush=True)
                return candidate
            print(f"[insights repair] {directory.name}: {len(errors)} issues", flush=True)
            feedback = "\n\n修复以下候选的实质问题，仅改被指出的问题字段，不改原故事。以下是候选与待复核的修复建议：\n"
            feedback += json.dumps({"candidate": candidate, "issues": errors}, ensure_ascii=False)
        raise ValueError("Architecture/closing failed review after bounded repairs; no new insights published")
    except Exception as error:
        receipt.update(status="failed", error=str(error))
        raise
    finally:
        receipt["calls"] = client.calls
        write_json(attempt_path, receipt)


def main():
    parser = argparse.ArgumentParser(description="Generate evidence-backed architecture and closing for any prepared story.")
    parser.add_argument("directories", type=Path, nargs="+")
    parser.add_argument("--model")
    parser.add_argument("--gh-host")
    arguments = parser.parse_args()
    for directory in arguments.directories:
        generate_insights(directory, model=arguments.model, gh_host=arguments.gh_host)


if __name__ == "__main__":
    main()
