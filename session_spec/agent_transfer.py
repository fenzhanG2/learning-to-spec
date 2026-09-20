import json
import re
from collections import Counter
from .agent_evidence import EvidenceIndex, handoff_phase


def validate_transfer(detail, events):
    evidence = {event["ref"]: event for event in events}
    errors = []

    def fields(value, required, label):
        if not isinstance(value, dict):
            errors.append(label + " must be an object")
            return False
        for field in required:
            if not isinstance(value.get(field), str) or not value[field].strip():
                errors.append(label + " needs " + field)
        refs = value.get("refs")
        if not isinstance(refs, list) or not refs or any(not isinstance(ref, str) or ref not in evidence for ref in refs):
            errors.append(label + " needs real source refs")
        return True

    fields(detail.get("resume"), ("checkpoint", "workspace", "next_action", "verification_boundary"), "Agent resume")
    for section in ("continuation", "recipes"):
        items = detail.get(section)
        if not isinstance(items, list):
            errors.append("Agent " + section + " must be an array; empty is valid when unsupported")
            continue
        for item in items:
            required = (("title", "trigger", "done_when", "stop_when") if section == "continuation"
                        else ("title", "when", "adapt", "avoid", "verify"))
            if not fields(item, required, "Agent " + section):
                continue
            if item.get("basis") not in {"explicit", "proposed"}:
                errors.append("Agent " + section + " basis must be explicit or proposed; reuse is not a future success guarantee")
            if section == "recipes":
                procedure = item.get("procedure")
                if not isinstance(procedure, list) or not procedure or any(not isinstance(step, str) or not step.strip() for step in procedure):
                    errors.append("Agent recipe needs an ordered procedure")
            else:
                steps = item.get("steps")
                if not isinstance(steps, list) or not steps:
                    errors.append("Agent continuation needs concrete steps")
                    continue
                for step in steps:
                    if fields(step, ("action", "precondition", "expected", "otherwise"), "Agent continuation step"):
                        if step.get("kind") not in {"inspect", "verify", "change", "external"}:
                            errors.append("Agent continuation step kind must be inspect/verify/change/external")
    requests = Counter(event.get("tool_call_id") for event in events if event.get("type") == "tool.execution_start" and event.get("tool_call_id"))
    used = set()
    for phase in detail.get("trajectory", []):
        if not isinstance(phase, dict):
            continue
        steps = phase.get("tool_steps")
        if not isinstance(steps, list):
            errors.append("Agent phase needs tool_steps, not a detached list of tool refs")
            continue
        if phase.get("tool_refs") and not steps:
            errors.append("Agent phase with tools needs at least one meaningful inline tool step")
        for step in steps:
            if not fields(step, ("purpose", "finding", "decision"), "Agent tool step"):
                continue
            refs = step.get("tool_refs")
            if not isinstance(refs, list) or not refs or any(not isinstance(ref, str) or ref not in evidence or not evidence[ref].get("type", "").startswith("tool.") for ref in refs):
                errors.append("Agent tool step needs real tool refs")
                continue
            phase_refs = phase.get("tool_refs")
            if not isinstance(phase_refs, list) or not set(refs).issubset(phase_refs):
                errors.append("Agent phase tool_refs must include its inline step tool_refs: " + str(phase.get("id")))
            units = {("call", evidence[ref]["tool_call_id"]) if requests.get(evidence[ref].get("tool_call_id")) == 1 else ("event", ref) for ref in refs}
            if used.intersection(units):
                errors.append("Agent tool steps repeat a tool call or split its request/result; put the pair in one step and refer back later")
            used.update(units)
    return errors


def inline_tool_plan(detail, events, ledger):
    positions = {event["ref"]: index for index, event in enumerate(events)}
    units = [{**call, "source_ref": call["request_ref"]} for call in ledger["calls"]]
    units += [{"source_ref": event["ref"], "request_ref": None, "tool": event.get("tool", "unknown"),
               "arguments": None, "results": [event], "status": "unpaired_result"} for event in ledger["unpaired_results"]]
    units.sort(key=lambda unit: positions[unit["source_ref"]])
    by_ref = {}
    for index, unit in enumerate(units):
        for ref in [unit["source_ref"], *[event["ref"] for event in unit["results"]]]:
            by_ref[ref] = index
    phases = detail["trajectory"]
    starts = [min((positions[ref] for ref in phase["refs"] if ref in positions), default=0) for phase in phases]
    plan = [{"steps": [[] for step in phase["tool_steps"]], "remaining": []} for phase in phases]
    assigned = set()
    for phase_index, phase in enumerate(phases):
        for step_index, step in enumerate(phase["tool_steps"]):
            indices = sorted({by_ref[ref] for ref in step["tool_refs"] if ref in by_ref})
            for index in indices:
                if index not in assigned:
                    plan[phase_index]["steps"][step_index].append(units[index])
                    assigned.add(index)
    for index, unit in enumerate(units):
        if index in assigned:
            continue
        refs = {unit["source_ref"], *[event["ref"] for event in unit["results"]]}
        explicit = [phase_index for phase_index, phase in enumerate(phases) if refs.intersection(phase["tool_refs"])]
        preceding = [phase_index for phase_index, start in enumerate(starts) if start <= positions[unit["source_ref"]]]
        phase_index = explicit[0] if explicit else (preceding[-1] if preceding else 0)
        plan[phase_index]["remaining"].append(unit)
    return plan


def excerpt(value, limit):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
    if len(text) <= limit:
        return text
    return text[:limit // 2] + "\n[excerpt; omitted content in _support/tool-ledger.json]\n" + text[-limit // 2:]


def fenced(text):
    fence = "`" * max(3, 1 + max((len(match) for match in re.findall(r"`+", text)), default=0))
    return fence + "text\n" + text + "\n" + fence


def result_payload(event):
    return {key: event[key] for key in ("success", "error", "result", "text") if key in event}


def decision_phase(phase, ledger, label):
    names = {}
    for call in ledger["calls"]:
        for ref in [call["request_ref"], *[result["ref"] for result in call["results"]]]:
            names[ref] = call["tool"]
    names.update({event["ref"]: event.get("tool", "unknown") for event in ledger["unpaired_results"]})
    lines = ["### " + phase["id"] + " — " + phase["title"], "", phase["summary"], ""]
    refs = [*phase["human_refs"], *phase["refs"]]
    for index, step in enumerate(phase["tool_steps"], 1):
        tools = list(dict.fromkeys(names[ref] for ref in step["tool_refs"] if names.get(ref) not in (None, "unknown")))
        tools_label = " · " + label("Tools: ", "工具：") + ", ".join(tools) if tools else ""
        lines.extend([str(index) + ". **" + step["purpose"] + "**" + tools_label, "",
                      "   " + step["finding"], "",
                      "   " + label("Decision: ", "决策：") + step["decision"], ""])
        refs.extend([*step["refs"], *step["tool_refs"]])
    rationale = phase["rationale"]
    if rationale["basis"] != "not_recorded":
        lines.extend([label("Rationale", "依据") + " [" + rationale["basis"] + "]: " + rationale["text"], ""])
    refs.extend(rationale["refs"])
    lines.extend([label("Outcome", "结果") + " [" + phase["outcome"] + "]: " + phase["observation"], "",
                  label("Next state: ", "后续状态：") + phase["next_state"], "",
                  label("Sources: ", "来源：") + ", ".join(dict.fromkeys(refs)), ""])
    return lines


def render_transfer(article, events, ledger, language, trajectory_style="decisions"):
    payload_aware = trajectory_style == "handoff-portable-v2"
    portable = trajectory_style in {"handoff-portable", "handoff-portable-v2"}
    separate = trajectory_style == "handoff-split" or portable
    if separate:
        trajectory_style = "handoff"
    legacy_roles = trajectory_style == "handoff-legacy"
    if legacy_roles:
        trajectory_style = "handoff"
    if trajectory_style not in {"decisions", "expanded", "handoff"}:
        raise ValueError("Unknown trajectory rendering style")
    detail = article["agent_detail"]
    label = lambda english, chinese: chinese if language.startswith("zh") else english
    evidence_index = EvidenceIndex(events, ledger, language, legacy_roles=legacy_roles, portable=portable, payload_aware=payload_aware) if trajectory_style == "handoff" else None
    cite = evidence_index.cite if evidence_index else lambda refs: ", ".join(refs)
    sources_label = label("Selected evidence: ", "关键来源：") if evidence_index else label("Sources: ", "来源：")
    resume = detail["resume"]
    markdown = article["agent_markdown"].strip()
    title, _, body = markdown.partition("\n")
    lines = [title, "", "## " + label("Resume here", "从这里接手"), ""]
    for key, english, chinese in (("checkpoint", "Last known state", "最后已知状态"), ("workspace", "Workspace and portability", "工作区与迁移条件"),
                                  ("next_action", "First useful move", "第一步有效动作"), ("verification_boundary", "Already established / still unverified", "已确认／尚未验证")):
        lines.extend(["**" + label(english, chinese) + "**: " + resume[key], ""])
    lines.extend([sources_label + cite(resume["refs"]), "", body.strip(), ""])
    if detail["continuation"]:
        lines.extend(["## " + label("Continue this task", "继续当前任务"), ""])
    for route in detail["continuation"]:
        lines.extend(["### " + route["title"], "", label("Trigger", "触发条件") + " [" + route["basis"] + "]: " + route["trigger"], ""])
        for index, step in enumerate(route["steps"], 1):
            lines.extend([f"{index}. [{step['kind']}] " + step["action"], "",
                          "   - " + label("Before: ", "前提：") + step["precondition"],
                          "   - " + label("Expected: ", "预期：") + step["expected"],
                          "   - " + label("If not: ", "否则：") + step["otherwise"],
                          "   - " + label("Basis: ", "依据：") + cite(step["refs"]), ""])
        lines.extend([label("Goal-level acceptance: ", "目标层验收：") + route["done_when"], "",
                      label("Pause / stop / ask when: ", "暂停／停止／询问条件：") + route["stop_when"], "",
                      sources_label + cite(route["refs"]), ""])
    if detail["recipes"]:
        lines.extend(["## " + label("Solve a similar task", "解决相似任务"), ""])
    for recipe in detail["recipes"]:
        lines.extend(["### " + recipe["title"] + " [" + recipe["basis"] + "]", "",
                      label("Use when: ", "适用条件：") + recipe["when"], "", label("Adapt first: ", "先替换／核对：") + recipe["adapt"], ""])
        lines.extend(str(index) + ". " + step for index, step in enumerate(recipe["procedure"], 1))
        lines.extend(["", label("Avoid / failure fingerprints: ", "避免／失败特征：") + recipe["avoid"], "",
                      label("Validate: ", "验证：") + recipe["verify"], "", sources_label + cite(recipe["refs"]), ""])
    if evidence_index:
        lines.extend(["## " + label("Detailed trajectory", "详细工作轨迹"), "",
                      (label("Grouped historical tool usage: action → finding → decision, not every invocation. E-number links identify source events and open the optional [evidence companion](evidence.md). History is not fresh authorization; rationale is an observable decision summary, not hidden chain-of-thought.",
                             "按行动→发现→决策概述历史工具使用，不逐条重放调用。E 编号指向源事件，点击可按需查阅 [证据附件](evidence.md)。历史不是新的授权；依据是可观察决策摘要，不是隐藏思维链。") if separate else
                       label("Tool usage below summarizes historical actions, targets and observations, not every invocation. Named evidence links resolve to source notes in this Markdown. Recheck present state before acting; history is not fresh authorization.",
                             "以下概述历史工具的操作、对象与观察，不逐条重放调用。具名证据链接指向本 Markdown 内的来源说明。行动前核查当前状态；历史记录不是新的授权。")), ""])
    elif trajectory_style == "decisions":
        lines.extend(["## " + label("Decision trajectory", "决策轨迹"), "",
                      label("Key actions, findings and changes of direction; routine tool calls and raw payloads stay in the evidence attachments. Historical actions are not a replay script. Rationale summarizes observable evidence, not hidden chain-of-thought.",
                            "只保留关键行动、发现和转折；常规工具调用与原始载荷留在证据附件中。历史动作不是重放脚本；依据是可观察证据的总结，不是隐藏思维链。"), ""])
    else:
        lines.extend(["## " + label("Detailed trajectory", "详细工作轨迹"), "",
                      label("Historical tool calls below are evidence, not a replay script. Unknown results stay unknown. Decision summaries describe observable rationale, not hidden chain-of-thought.",
                            "以下历史工具调用是证据，不是重放脚本。未知结果保持未知；决策摘要描述可观察依据，不是隐藏思维链。"), ""])
    plan = inline_tool_plan(detail, events, ledger) if trajectory_style == "expanded" else [None] * len(detail["trajectory"])
    for phase, placement in zip(detail["trajectory"], plan):
        if evidence_index:
            lines.extend(handoff_phase(phase, evidence_index))
            continue
        if trajectory_style == "decisions":
            lines.extend(decision_phase(phase, ledger, label))
            continue
        lines.extend(["### " + phase["id"] + " — " + phase["title"], "", phase["summary"], ""])
        for step, units in zip(phase["tool_steps"], placement["steps"]):
            lines.extend(["**" + label("Question / purpose", "问题／目的") + "**: " + step["purpose"], ""])
            for unit in units:
                lines.extend(["**" + label("Historical tool", "历史工具") + " — " + str(unit["tool"]) + " · " + unit["source_ref"] + "** [" + unit["status"] + "]", ""])
                if unit["request_ref"]:
                    arguments = unit["arguments"]
                    limit = 6000 if isinstance(arguments, dict) and any(key in arguments for key in ("command", "cmd")) else 1200
                    lines.extend([fenced(excerpt(arguments, limit)), ""])
                for result in unit["results"]:
                    lines.extend([label("Recorded result: ", "记录结果：") + result["ref"], "", fenced(excerpt(result_payload(result), 600)), ""])
                if not unit["results"]:
                    lines.extend([label("No paired result was recorded.", "没有配对的结果记录。"), ""])
            lines.extend(["**" + label("What this established", "确认了什么") + "**: " + step["finding"], "",
                          "**" + label("Decision / consequence", "决策／后果") + "**: " + step["decision"], "",
                          label("Sources: ", "来源：") + ", ".join(step["refs"]), ""])
        if placement["remaining"]:
            lines.extend(["#### " + label("Other tool activity near this phase", "本阶段附近的其他工具活动"), "",
                          label("Source-order placement is navigation only, not a claim of causal relevance. Payload excerpts are not runnable commands.",
                                "按源记录顺序就近归档只帮助导航，不推断因果关系。载荷摘录不是可执行命令。"), ""])
            for unit in placement["remaining"]:
                payload = {"arguments": unit["arguments"], "results": [{"ref": result["ref"], **result_payload(result)} for result in unit["results"]]}
                lines.extend(["- " + unit["source_ref"] + " · " + str(unit["tool"]) + " [" + unit["status"] + "]", "", fenced(excerpt(payload, 450)), ""])
        rationale = phase["rationale"]
        lines.extend([label("Phase rationale", "阶段依据") + " [" + rationale["basis"] + "]: " + rationale["text"], "",
                      label("Outcome", "状态") + " [" + phase["outcome"] + "]: " + phase["observation"], "",
                      label("State transition: ", "状态变化：") + phase["next_state"], "",
                      label("User inputs: ", "用户输入：") + (", ".join(phase["human_refs"]) or "—"), "",
                      label("Sources: ", "来源：") + ", ".join(dict.fromkeys([*phase["refs"], *rationale["refs"]])), ""])
    lines.extend(["## " + label("Successful, failed and unresolved paths", "成功、失败与未收口路径"), ""])
    for path in detail["paths"]:
        lines.extend(["### " + path["title"] + " [" + path["outcome"] + "]", "", path["reason"], "",
                      label("Reuse / avoid / recheck: ", "复用／避免／重查：") + path["reuse_condition"], "",
                      " → ".join(path["phase_ids"]) + "; " + cite(path["refs"]), ""])
    if portable:
        lines.extend(["## " + label("Evidence and transfer", "证据与交付"), "",
                      label("Start with this handoff; it contains the working state, next actions, decision trajectory and reusable methods. Keep `agent-spec.md` and [evidence.md](evidence.md) together; the companion is optional source lookup. Private export internals are not delivered and are not required to continue. Check the current workspace before acting: historical paths, commands and approvals are not portable defaults or new authority.",
                            "先读本交接文档，工作状态、下一步、决策轨迹和复用方法均在正文。将 `agent-spec.md` 与 [evidence.md](evidence.md) 放在同一目录；附件仅供按需回查。私有导出内部材料不随包交付，也不是继续工作所必需的。操作前核对当前工作区：历史路径、命令和批准不是可直接迁移的默认值或新的授权。"), ""])
    elif separate:
        lines.extend(["## " + label("Evidence and transfer", "证据与交付"), "",
                      label("Start with this handoff; it contains the working state, next actions, decision trajectory and reusable methods. Transfer `agent-spec.md` with [evidence.md](evidence.md) in the same directory for optional source lookup. Exact payloads and provenance are in `_support/`; they are not required first reading. Historical machine paths and approvals are not portable defaults or new authority.",
                            "先读本交接文档，工作状态、下一步、决策轨迹和复用方法均在正文。交付时把 `agent-spec.md` 与 [evidence.md](evidence.md) 放在同一目录，按需查来源。精确载荷与来源元数据保留在 `_support/`，不是接手前置阅读。历史机器路径和批准不是可直接迁移的默认值或新授权。"), ""])
    elif trajectory_style in {"decisions", "handoff"}:
        lines.extend(["## " + label("Evidence and transfer", "证据与交付"), "",
                      label("This Markdown carries the working contract, continuation branches and decision trajectory, not a tool transcript. For exact lookup by source ref, attach `_support/tool-ledger.json`, `_support/evidence.jsonl` and `_support/source.json`; these retain full sanitized payloads, including unpaired or unknown results. Upstream omissions and redactions remain limitations. Re-establish current workspace state before acting; historical actions are not new authority.",
                            "本 Markdown 包含工作约定、接续分支和决策轨迹，不是工具逐条记录。按来源引用精确回查时，可附上 `_support/tool-ledger.json`、`_support/evidence.jsonl` 和 `_support/source.json`；其中保留完整脱敏载荷，包括未配对或未知结果。上游缺失和脱敏仍是限制。操作前重新确认当前工作区状态；历史动作不是新的授权。"), ""])
    else:
        lines.extend(["## " + label("Evidence and transfer", "证据与交付"), "",
                      label("This Markdown carries the working contract, decision branches and inline tool evidence. Attach `_support/tool-ledger.json`, `_support/evidence.jsonl` and `_support/source.json` for full sanitized payloads and provenance. No tail-only tool index is required. Missing upstream content, images, unpaired results and redactions remain limitations; do not reconstruct them. Re-establish current workspace state before acting; historical actions are not new authority.",
                            "本 Markdown 包含工作约定、判断分支和随文工具证据。附上 `_support/tool-ledger.json`、`_support/evidence.jsonl` 和 `_support/source.json` 可回查完整脱敏载荷与来源，不依赖文末工具表。上游缺失、缺图、未配对结果和脱敏仍是限制，不补造。操作前重新确认当前工作区状态；历史动作不是新的授权。"), ""])
    result = "\n".join(lines)
    return evidence_index.finish(result, separate=separate) if evidence_index else result
