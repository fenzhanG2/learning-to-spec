import json
import re
from collections import Counter

from .agent_transfer import render_transfer, validate_transfer
from .agent_evidence import validate_usage

OUTCOMES = {"verified", "partial", "failed", "abandoned", "unresolved"}


def validate_agent_detail(detail, events):
    if not isinstance(detail, dict) or detail.get("schema") not in {"agent-detail/v1", "agent-detail/v2", "agent-detail/v3"}:
        return ["Detailed Agent handoff requires agent_detail with schema agent-detail/v1, v2 or v3"]
    evidence = {event["ref"]: event for event in events}
    human_refs = {event["ref"] for event in events if event.get("human_input")}
    errors, covered, phase_ids = [], set(), set()
    phases = detail.get("trajectory")
    paths = detail.get("paths")
    if not isinstance(phases, list) or not phases or not isinstance(paths, list) or not paths:
        return ["Agent detail needs nonempty trajectory and paths arrays; use unresolved rather than inventing success/failure"]

    def refs(value, label, allowed=None, nonempty=True):
        if (not isinstance(value, list) or (nonempty and not value) or
                any(not isinstance(ref, str) or ref not in evidence or (allowed is not None and ref not in allowed) for ref in value)):
            errors.append("Invalid Agent references: " + label + "; supplied=" + json.dumps(value, ensure_ascii=False))
            return []
        return value

    tool_refs = {ref for ref, event in evidence.items() if event.get("type", "").startswith("tool.")}
    positions = {event["ref"]: index for index, event in enumerate(events)}
    previous = -1
    for phase_index, phase in enumerate(phases):
        if not isinstance(phase, dict):
            errors.append("Agent phase must be an object")
            continue
        identifier = phase.get("id")
        if not isinstance(identifier, str) or not re.fullmatch(r"[a-z][a-z0-9-]*", identifier) or identifier in phase_ids:
            errors.append("Agent phase IDs must be unique ASCII slugs")
        else:
            phase_ids.add(identifier)
        for field in ("title", "summary", "observation", "next_state"):
            if not isinstance(phase.get(field), str) or not phase[field].strip():
                errors.append("Missing Agent phase field: " + field)
        if phase.get("outcome") not in OUTCOMES:
            errors.append("Invalid Agent phase outcome")
        anchors = refs(phase.get("refs"), "phase")
        if anchors:
            start = min(positions[ref] for ref in anchors)
            if start < previous:
                errors.append("Agent trajectory must follow source chronology; do not repeat old opening refs on later phases. Phase=" + str(identifier) + "; earliest supplied ref=" + min(anchors, key=lambda ref: positions[ref]) + "; previous phase starts at source position=" + str(previous))
            previous = start
        covered.update(refs(phase.get("human_refs"), "phase human_refs", human_refs, False))
        refs(phase.get("tool_refs"), "phase " + str(identifier) + " tool_refs (must be real tool events, not assistant reports)", tool_refs, False)
        rationale = phase.get("rationale")
        if not isinstance(rationale, dict) or rationale.get("basis") not in {"recorded", "inferred", "not_recorded"} or not isinstance(rationale.get("text"), str) or (rationale["basis"] != "not_recorded" and not rationale["text"].strip()):
            errors.append(f"/article/agent_detail/trajectory/{phase_index}/rationale needs basis recorded/inferred/not_recorded; recorded/inferred require nonempty text and real refs. not_recorded may use text='' and refs=[] and is omitted from the v3 reader view.")
        else:
            refs(rationale.get("refs"), "rationale", nonempty=rationale["basis"] != "not_recorded")
            if rationale["basis"] == "not_recorded" and rationale.get("refs") != []:
                errors.append(f"/article/agent_detail/trajectory/{phase_index}/rationale: not_recorded must have refs=[]; do not imply evidence for an absent rationale")
    missing = human_refs - covered
    if missing:
        errors.append("Agent trajectory omits user inputs (short corrections/acknowledgements count): " + ", ".join(sorted(missing)))
    for path in paths:
        if not isinstance(path, dict):
            errors.append("Agent path must be an object")
            continue
        for field in ("title", "reason", "reuse_condition"):
            if not isinstance(path.get(field), str) or not path[field].strip():
                errors.append("Missing Agent path field: " + field)
        if path.get("outcome") not in OUTCOMES:
            errors.append("Invalid Agent path outcome")
        linked = path.get("phase_ids")
        if not isinstance(linked, list) or not linked or any(not isinstance(identifier, str) or identifier not in phase_ids for identifier in linked):
            errors.append("Agent path must link to real trajectory phase IDs")
        refs(path.get("refs"), "path")
    if detail["schema"] in {"agent-detail/v2", "agent-detail/v3"}:
        errors.extend(validate_transfer(detail, events))
    if detail["schema"] == "agent-detail/v3" and not errors:
        errors.extend(validate_usage(detail, events, tool_ledger(events)))
    return errors


def tool_ledger(events):
    requests = [event for event in events if event.get("type") == "tool.execution_start"]
    results = [event for event in events if event.get("type") == "tool.execution_complete"]
    counts = Counter(event.get("tool_call_id") for event in requests if event.get("tool_call_id"))
    linked, calls = set(), []
    for request in requests:
        call_id = request.get("tool_call_id")
        matches = [event for event in results if call_id and counts[call_id] == 1 and event.get("tool_call_id") == call_id]
        linked.update(event["ref"] for event in matches)
        status = "no_result" if not matches else "result_recorded"
        if matches and any(event.get("success") is False or event.get("error") for event in matches):
            status = "explicit_failure"
        elif matches and all(event.get("success") is True for event in matches):
            status = "tool_success_not_acceptance"
        calls.append({"request_ref": request["ref"], "tool": request.get("tool", "unknown"), "call_id": call_id,
                      "turn": request.get("turn"), "timestamp": request.get("timestamp"), "arguments": request.get("arguments"),
                      "results": matches, "status": status})
    return {"schema": "tool-ledger/v1", "calls": calls, "unpaired_results": [event for event in results if event["ref"] not in linked],
            "limit": "Request/result linkage only; unknown success stays unknown. Full sanitized payloads are retained. Nothing was executed."}


def compact_argument(arguments, limit=280):
    if isinstance(arguments, dict):
        focus = {key: arguments[key] for key in ("command", "cmd", "file_path", "path", "pattern", "query", "url", "skill") if key in arguments}
        content = json.dumps(focus or arguments, ensure_ascii=False, separators=(",", ":"))
    else:
        content = json.dumps(arguments, ensure_ascii=False)
    content = content.replace("|", "\\|").replace("\n", " ")
    return content if len(content) <= limit else content[:limit] + " … [excerpt; full arguments in tool-ledger.json]"


def render_agent(article, events, language="en", trajectory_style=None):
    detail = article.get("agent_detail")
    if not detail:
        return article["agent_markdown"]
    if detail.get("schema") in {"agent-detail/v2", "agent-detail/v3"}:
        style = trajectory_style or ("handoff-portable-v4" if detail["schema"] == "agent-detail/v3" else "decisions")
        return render_transfer(article, events, tool_ledger(events), language, style)
    chinese = language.startswith("zh")
    label = lambda english, translated: translated if chinese else english
    lines = [article["agent_markdown"].rstrip(), "", "## " + label("Detailed trajectory", "详细工作轨迹"), "",
             label("The following are observable actions and evidence-based decision summaries, not hidden chain-of-thought. Historical actions are not fresh execution authority.",
                   "以下记录可观察动作和有证据的决策摘要，不是隐藏思维链；历史操作不是当前执行授权。"), ""]
    for phase in detail["trajectory"]:
        rationale = phase["rationale"]
        lines.extend(["### " + phase["id"] + " — " + phase["title"], "", phase["summary"], "",
                      "- " + label("Trigger / user inputs: ", "触发／用户输入：") + (", ".join(phase["human_refs"]) or "—"),
                      "- " + label("Tool evidence: ", "工具证据：") + (", ".join(phase["tool_refs"]) or "—"),
                      "- " + label("Decision rationale", "决策依据") + " [" + rationale["basis"] + "]: " + rationale["text"] + " (" + ", ".join(rationale["refs"]) + ")",
                      "- " + label("Observed result: ", "实际结果：") + phase["observation"],
                      "- " + label("Outcome: ", "状态：") + phase["outcome"],
                      "- " + label("State transition / next: ", "状态变化／后续：") + phase["next_state"],
                      "- " + label("Sources: ", "来源：") + ", ".join(phase["refs"]), ""])
    lines.extend(["## " + label("Successful, failed and unresolved paths", "成功、失败与未收口路径"), ""])
    for path in detail["paths"]:
        lines.extend(["### " + path["title"] + " [" + path["outcome"] + "]", "", path["reason"], "",
                      label("Reuse / avoid / recheck: ", "复用／避免／重查：") + path["reuse_condition"], "",
                      label("Phases: ", "阶段：") + " → ".join(path["phase_ids"]) + "; " + ", ".join(path["refs"]), ""])
    ledger = tool_ledger(events)
    lines.extend(["## " + label("Complete tool-use index", "完整工具调用索引"), "",
                  label("Every recorded request appears below, in source order. The argument column is a navigation excerpt, not a runnable script. Full arguments and results are in `_support/tool-ledger.json`; every source event is in `_support/evidence.jsonl`. `result_recorded` does not imply success; `tool_success_not_acceptance` does not imply feature acceptance.",
                        "按原始顺序列出每次工具请求。参数列只是导航摘录，不是执行脚本；完整参数和结果见 `_support/tool-ledger.json`，所有源事件见 `_support/evidence.jsonl`。`result_recorded` 不等于成功，`tool_success_not_acceptance` 不等于功能验收。"), "",
                  "| " + label("Request / turn | Tool | Arguments (excerpt) | Results | Recorded status", "请求／轮次 | 工具 | 参数（摘录） | 结果 | 记录状态") + " |",
                  "| --- | --- | --- | --- | --- |"])
    for call in ledger["calls"]:
        lines.append("| " + call["request_ref"] + " / " + str(call["turn"]) + " | " + str(call["tool"]).replace("|", "\\|") + " | "
                     + compact_argument(call["arguments"]) + " | " + (", ".join(event["ref"] for event in call["results"]) or "—") + " | " + call["status"] + " |")
    if ledger["unpaired_results"]:
        lines.extend(["", label("Unpaired or ambiguous results (not silently discarded): ", "未配对或有歧义的结果（未丢弃）：")
                      + ", ".join(event["ref"] for event in ledger["unpaired_results"])])
    lines.extend(["", "## " + label("Evidence and transfer", "证据与交付"), "",
                  label("Transfer this Markdown with `_support/evidence.jsonl`, `_support/source.json` and `_support/tool-ledger.json` for exact lookup by event ref. The Markdown carries the working narrative; attachments carry full sanitized source payloads, not additional execution authority. Upstream truncation, missing images and redaction remain limitations. Do not reconstruct missing content.",
                        "交付本 Markdown 时一并提供 `_support/evidence.jsonl`、`_support/source.json` 和 `_support/tool-ledger.json`，按 ref 回查。Markdown 承载工作叙述，附件保留完整脱敏源数据，不提供额外执行授权。上游截断、缺图和脱敏限制仍存在，不补造缺失内容。"), ""])
    return "\n".join(lines)
