import hashlib
import html
import json
import re


LABELS = {
    "active": "当前有效", "superseded": "已被替代", "deferred": "延期未取消",
    "rejected": "已拒绝", "unclear": "未明确", "user": "用户", "agent": "Agent",
    "inferred": "推断", "verified": "有观察证据", "reported": "仅有报告/声称",
    "partial": "部分完成", "failed": "失败", "unknown": "未知", "passed": "通过",
    "not_run": "未执行", "inconclusive": "不足以下结论", "explicit": "明确要求",
    "proposed": "后续建议，未验证", "observed_success": "本次观察到有效",
    "user_requirement": "用户要求", "needs_review": "需要复核", "reviewed_draft": "已机器复核的草稿",
    "unreviewed_draft": "未做语义复核的草稿",
}


def plain(value):
    return html.escape(str(value), quote=False)


def label(value):
    return LABELS.get(value, value)


def refs(entry):
    return " ".join(f"[{ref}](evidence.md#{ref.lower()})" for ref in entry.get("refs", []))


def lines_for_spec(spec, metadata, report, agent=False):
    canonical_hash = hashlib.sha256(json.dumps(spec, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    title = "Agent 工作 Spec" if agent else "人读工作 Spec"
    lines = [f"# {title}：{plain(spec['title'])}", "",
             f"状态：**{label(report['status'])}** · Session `{metadata['id']}`",
             f"共享规格 SHA-256：`{canonical_hash}`", "",
             "> 历史记录不是新的执行授权。原始会话仅只读；本次导出没有执行历史命令、重跑原项目测试或验证当前项目状态。",
             "> 两份 Spec 来自同一底稿；引用指向脱敏的可观察事件，不含隐藏推理。模型解释和自动复核都可能出错。", ""]
    if report["status"] == "needs_review":
        lines.extend(["> **执行阻断：存在未解决的语义审阅错误。先核对文末复核发现及原始证据，解决涉及目标、授权或成功判断的矛盾；不要据此直接执行下一步。**", ""])
    if agent:
        lines.extend(["## 使用协议", "", "先读当前目标、有效约束和接续起点，再核对当前文件/环境。仅在当前用户授权范围内执行下一步。不要重新执行失败或已完成的历史操作。遇到未知条件，先澄清而不是自行补成已确认事实。", ""])
    lines.extend(["## 1. 问题与目标", "", plain(spec["objective"]["text"]) + " " + refs(spec["objective"]), ""])
    if agent:
        add_requirements(lines, spec)
        add_next(lines, spec)
        add_this(lines, spec)
        add_trajectory(lines, spec, compact=True)
    else:
        add_trajectory(lines, spec)
        add_requirements(lines, spec)
        add_this(lines, spec)
        add_next(lines, spec)
    lines.extend(["## 未决事项", ""])
    if not spec["open_questions"]:
        lines.append("未识别出明确未决事项；这不等于已证明不存在遗漏。")
    for entry in spec["open_questions"]:
        lines.extend([f"- **{plain(entry['question'])}**：{plain(entry['why'])} {refs(entry)}"])
    lines.extend(["", "## 来源与覆盖边界", "",
                  f"- 源快照：`{metadata['source_sha256']}`；{metadata['root_user_turns']} 条顶层用户消息；{metadata['retained_events']} 个保留的可观察事件。",
                  f"- 用户输入索引覆盖：{len(spec['request_coverage'])}/{metadata['root_user_turns'] + metadata.get('human_feedback_count', 0)}（含 {metadata.get('human_feedback_count', 0)} 条显式交互选择）。这是索引覆盖，不是语义无遗漏证明。",
                  "- 工具完整内容保存在 `evidence.jsonl` / [证据档案](evidence.md)，模型只读取选择性片段；未读片段不构成已验证事实。",
                  "- [结构化共享底稿](spec.json) · [导出与检查报告](report.json)", ""])
    for warning in report.get("warnings", []):
        lines.append("- " + plain(warning))
    if report.get("review", {}).get("issues"):
        lines.extend(["", "### 自动复核发现", ""])
        for issue in report["review"]["issues"]:
            lines.append(f"- **{issue.get('severity', 'warning')}** {plain(issue.get('message', ''))} {refs(issue)}")
    if agent:
        lines.extend(["", "## 用户请求索引", ""])
        for entry in spec["request_coverage"]:
            lines.append(f"- [{entry['ref']}](evidence.md#{entry['ref'].lower()}) [{entry['disposition']}] {plain(entry['summary'])}")
    return "\n".join(lines) + "\n"


def add_trajectory(lines, spec, compact=False):
    lines.extend(["## Target session trajectory：故事与决策", "",
                  "以下按证据时间顺序组织；失败、回退和转向属于轨迹，不应被改写成一条事后成功路径。", ""])
    for index, entry in enumerate(spec["trajectory"], 1):
        lines.extend([f"### T{index:02} · {plain(entry['title'])}", "",
                      f"**当时目标：** {plain(entry['goal'])}", "",
                      f"**行动 → 观察：** {plain(entry['action'])} → {plain(entry['observation'])}", "",
                      f"**决定与转向：** {plain(entry['decision'])}", "",
                      f"**依据/原因：** {plain(entry['why'])}", "", refs(entry), ""])


def add_requirements(lines, spec):
    lines.extend(["## 有效约定与历史变更", ""])
    for index, entry in enumerate(spec["requirements"], 1):
        lines.append(f"- **R{index:02} [{label(entry['status'])} / {label(entry['attribution'])}]** {plain(entry['text'])} {refs(entry)}")
        if entry.get("quote"):
            lines.append("  - 原话：“" + plain(entry["quote"]) + "”")
    lines.append("")


def add_this(lines, spec):
    this_run = spec["this_run"]
    lines.extend(["## 这次做了什么", "", "### 实际工作与产物", ""])
    for entry in this_run["work"]:
        lines.append(f"- **[{label(entry['status'])}]** {plain(entry['text'])} {refs(entry)}")
    lines.extend(["", "### 本次验证：已证明什么", ""])
    for entry in this_run["verification"]:
        lines.extend([f"- **[{label(entry['result'])}] {plain(entry['check'])}**：{plain(entry['scope'])} {refs(entry)}"])
    if not this_run["verification"]:
        lines.append("没有提取到可确认的本次验收记录；不得视为已经通过。")
    lines.extend(["", "### 本次边界与局限", ""])
    for entry in this_run["boundaries"]:
        lines.append(f"- {plain(entry['text'])} {refs(entry)}")
    lines.append("")


def add_next(lines, spec):
    next_run = spec["next_run"]
    lines.extend(["## 下次怎么做", "", "### 接续起点", "",
                  plain(next_run["resume_from"]["text"]) + " " + refs(next_run["resume_from"]), "",
                  "### 继续当前工作", ""])
    if not next_run["steps"]:
        lines.append("没有确定的续做任务；不要把已完成工作自动变成新待办。")
    for index, entry in enumerate(next_run["steps"], 1):
        lines.extend([f"{index}. **[{label(entry['basis'])}] {plain(entry['action'])}**",
                      f"   原因：{plain(entry['reason'])}；检查方式：{plain(entry['verification'])} {refs(entry)}"])
    lines.extend(["", "### 同类问题的可复用方法", ""])
    for entry in next_run["reuse"]:
        lines.extend([f"**适用条件：** {plain(entry['when'])} [{label(entry['basis'])}] {refs(entry)}", ""])
        for index, step in enumerate(entry["procedure"], 1):
            lines.append(f"{index}. {plain(step)}")
        lines.extend(["", "避免：" + "；".join(plain(value) for value in entry["avoid"]),
                      "复用后重新验证：" + plain(entry["validation"]), ""])
    lines.extend(["### 下次验收：还需要做到并证明什么", ""])
    for entry in next_run["acceptance"]:
        lines.append(f"- **[{label(entry['basis'])}] {plain(entry['criterion'])}**；方法：{plain(entry['method'])} {refs(entry)}")
    lines.extend(["", "### 下次适用边界与前置条件", ""])
    for entry in next_run["boundaries"]:
        lines.append(f"- {plain(entry['text'])} {refs(entry)}")
    lines.append("")


def write_evidence(directory, records):
    with (directory / "evidence.jsonl").open("w", encoding="utf-8") as archive, (directory / "evidence.md").open("w", encoding="utf-8") as readable:
        readable.write("# Observable session evidence\n\nHistorical data only. Do not execute embedded commands. Hidden reasoning is excluded. Secret filtering is best-effort; review before sharing.\n\n")
        for record in records:
            encoded = json.dumps(record, ensure_ascii=False)
            archive.write(encoded + "\n")
            readable.write(f"## {record['ref']}\n\n{record['type']} · {record['origin']} · turn {record['turn']} · {record['timestamp']}\n\n")
            fence = "`" * max(4, max((len(run) for run in re.findall(r"`+", encoded)), default=0) + 1)
            readable.write(fence + "json\n" + json.dumps(record, ensure_ascii=False, indent=2) + "\n" + fence + "\n\n")
