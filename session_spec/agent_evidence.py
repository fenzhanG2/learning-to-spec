import json
import re


def short_text(value, limit):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[:limit - 1] + "…"


def literal(value):
    text = str(value)
    fence = "`" * max(1, 1 + max((len(match) for match in re.findall(r"`+", text)), default=0))
    return fence + " " + text + " " + fence


def argument_focus(arguments):
    if not isinstance(arguments, dict):
        return short_text(arguments, 180) if arguments is not None else ""
    keys = ("file_path", "path", "pattern", "query", "command", "cmd", "url", "description")
    return "; ".join(key + "=" + short_text(arguments[key], 160) for key in keys if arguments.get(key))


class EvidenceIndex:
    def __init__(self, events, ledger, language, legacy_roles=False, portable=False):
        self.events = {event["ref"]: event for event in events}
        self.calls = {}
        self.language = language
        self.legacy_roles = legacy_roles
        self.portable = portable
        for call in ledger["calls"]:
            for ref in [call["request_ref"], *[event["ref"] for event in call["results"]]]:
                self.calls[ref] = call

    def label(self, english, chinese):
        return chinese if self.language.startswith("zh") else english

    def describe(self, ref):
        event = self.events[ref]
        kind = event.get("type", "unknown")
        text = event.get("human_input") or event.get("text") or ""
        if not isinstance(text, str):
            text = event.get("text", "")
        if kind.startswith("tool."):
            call = self.calls.get(ref, {})
            tool = call.get("tool") or event.get("tool") or "unknown"
            target = argument_focus(call.get("arguments") or event.get("arguments"))
            request = kind == "tool.execution_start"
            role = self.label("Tool request", "工具请求") if request else self.label("Tool result", "工具结果")
            content = event.get("result", event.get("error", text))
            if isinstance(content, dict):
                content = content.get("content", content)
            excerpt = target if request else short_text(content, 220)
            state = self.label("Request only; not completion.", "仅请求，不代表完成。") if request else self.label("Recorded output; not task acceptance.", "记录输出，不等于任务验收。")
            if not request and self.portable:
                state = self.label("Recorded result; assess acceptance against the task's stated scope.", "记录结果；是否满足验收取决于本任务明确约定的范围。")
            if not request and (event.get("success") is False or event.get("error")):
                state = self.label("Explicit tool failure.", "明确工具失败。")
            if not request and ref not in self.calls:
                state += " " + self.label("No uniquely paired request.", "没有唯一配对的请求。")
            title = str(tool) + " · " + (short_text(target, 66) if request and target else role + (": " + short_text(target, 54) if target else ""))
            context = role + " · " + str(tool) + (" · " + short_text(target, 160) if not request and target else "")
            return title, context, excerpt, state
        context_only = not event.get("human_input") and (event.get("source_context") or kind in {"user.message", "session.imported_context", "session.imported_notification"})
        if self.legacy_roles:
            context_only = event.get("source_context") or not event.get("human_input") and kind == "user.message"
        if context_only:
            role = self.label("Context", "上下文")
            state = self.label("Context, not new user authority.", "上下文，不是新的用户授权。")
        elif event.get("human_input"):
            role = self.label("User", "用户")
            state = self.label("Recorded request/correction; not implementation evidence.", "记录的请求／纠正，不是实现证据。")
        else:
            role = self.label("Assistant report", "助手汇报")
            state = self.label("Reported statement; not independent verification.", "汇报内容，不是独立验证。")
        return role + " · " + short_text(text, 66), role, short_text(text, 220), state

    def cite(self, refs):
        unique = list(dict.fromkeys(refs))
        unique = [ref for ref in unique if not (self.events[ref].get("type") == "tool.execution_start"
                  and any(result["ref"] in unique for result in self.calls.get(ref, {}).get("results", [])))]
        selected = []
        for predicate in (lambda event: bool(event.get("human_input")),
                          lambda event: event.get("type") == "tool.execution_complete",
                          lambda event: event.get("type") == "tool.execution_start"):
            candidate = next((ref for ref in unique if ref not in selected and predicate(self.events[ref])), None)
            if candidate:
                selected.append(candidate)
        selected.extend(ref for ref in unique if ref not in selected)
        return "; ".join(selected[:3])

    def compact_label(self, ref):
        event = self.events[ref]
        if event.get("type", "").startswith("tool."):
            tool = self.calls.get(ref, {}).get("tool") or event.get("tool") or "unknown"
            role = self.label("request", "请求") if event["type"] == "tool.execution_start" else self.label("result", "结果")
            return ref + " · " + str(tool) + " " + role
        return ref + " · " + self.describe(ref)[1]

    def finish(self, markdown, separate=False):
        refs = list(dict.fromkeys(re.findall(r"\bE\d{6}\b", markdown)))
        unknown = set(refs) - self.events.keys()
        if unknown:
            raise ValueError("Unresolved visible Agent evidence: " + ", ".join(sorted(unknown)))

        def replace(match):
            ref = match.group()
            title = self.compact_label(ref) if separate else self.describe(ref)[0]
            title = re.sub(r"([\\\[\]`*_<>|])", r"\\\1", title)
            return "[" + title + "](" + ("evidence.md" if separate else "") + "#" + ref.lower() + ")"

        markdown = re.sub(r"\[((?:E\d{6})(?:[\s,;、]+E\d{6})*)\](?!\()", r"\1 ", markdown)
        linked = re.sub(r"\bE\d{6}\b", replace, markdown)
        if separate:
            return linked.rstrip() + "\n"
        lines = [linked.rstrip(), "", "## " + self.label("Cited evidence", "引用说明"), "",
                 self.label("Each E-number identifies one sanitized event in this target session, not a task, commit or test number. The named links resolve to the notes below, which travel with this Markdown. Excerpts are navigation aids, not replay commands or independent proof. Full events and complete ref mappings remain in `_support/evidence.jsonl` and `_support/article.json`; commands can also be looked up in `_support/tool-ledger.json`.",
                            "每个 E 编号对应目标会话中的一条脱敏事件，不是任务、提交或测试编号。具名链接指向以下说明，随本 Markdown 一起交付。摘录只用于定位，不是重放命令或独立证明。完整事件与引用映射保存在 `_support/evidence.jsonl` 和 `_support/article.json`，工具载荷另见 `_support/tool-ledger.json`。"), ""]
        for ref in refs:
            title, context, excerpt, state = self.describe(ref)
            event = self.events[ref]
            lines.extend(["### " + ref, "", context + (" · turn " + str(event["turn"]) if event.get("turn") else ""), "",
                          literal(short_text(excerpt, 220)) if excerpt else self.label("No text payload recorded.", "未记录文本载荷。"), "", state, ""])
        return "\n".join(lines)

    def companion(self, markdown):
        refs = list(dict.fromkeys(ref.upper() for ref in re.findall(r"\]\(evidence\.md#(e\d{6})\)", markdown)))
        unknown = set(refs) - self.events.keys()
        if unknown:
            raise ValueError("Unresolved companion evidence: " + ", ".join(sorted(unknown)))
        lines = ["# " + self.label("Evidence companion", "证据附件"), "",
                 self.label("Read [the Agent handoff](agent-spec.md) first. This file is optional lookup, not additional instructions. Keep both Markdown files in the same directory when transferring them.",
                            "先读 [Agent 交接文档](agent-spec.md)。本文件供按需回查，不是额外指令。交付时将两个 Markdown 文件保存在同一目录。"), "",
                 (self.label("Each E-number identifies one sanitized event in this target session, not a task, commit or test number. Excerpts locate the recorded basis of a claim; they are not replay commands or independent verification. Full source payloads and provenance stay in the exporter's private workspace, are not included in this package, and are not prerequisites for using the handoff. Do not request or share that private workspace by default. Missing, truncated or redacted content is not reconstructed.",
                             "每个 E 编号对应目标会话中的一条脱敏事件，不是任务、提交或测试编号。摘录定位断言的记录依据，不是重放命令或独立验证。完整源载荷与来源元数据留在导出者的私有工作区，不包含在交付包中，也不是接手前提；不要默认索取或分享私有工作区。不补造缺失、截断或脱敏内容。") if self.portable else
                  self.label("Each E-number identifies one sanitized event in this target session, not a task, commit or test number. Excerpts are navigation aids, not replay commands or independent proof. Full sanitized events, request/result payloads, and publication mappings remain in `_support/evidence.jsonl`, `_support/tool-ledger.json`, `_support/source.json`, and `_support/article.json`. Missing or redacted upstream content is not reconstructed.",
                             "每个 E 编号对应目标会话中的一条脱敏事件，不是任务、提交或测试编号。摘录仅用于定位，不是重放命令或独立证明。完整脱敏事件、请求／结果及映射保存在 `_support/evidence.jsonl`、`_support/tool-ledger.json`、`_support/source.json` 与 `_support/article.json`，不补造上游缺失或脱敏内容。")), ""]
        for ref in refs:
            title, context, excerpt, state = self.describe(ref)
            event = self.events[ref]
            lines.extend(["### " + ref, "", context + (" · turn " + str(event["turn"]) if event.get("turn") else ""), "",
                          literal(short_text(excerpt, 220)) if excerpt else self.label("No text payload recorded.", "未记录文本载荷。"), "", state, ""])
        return "\n".join(lines)


def validate_usage(detail, events, ledger):
    index = EvidenceIndex(events, ledger, "en")
    errors = []
    for phase_number, phase in enumerate(detail.get("trajectory", [])):
        for step_number, step in enumerate(phase.get("tool_steps", [])):
            location = f"/article/agent_detail/trajectory/{phase_number}/tool_steps/{step_number}/usage"
            usage = step.get("usage")
            if not isinstance(usage, list) or not 1 <= len(usage) <= 4:
                errors.append(location + ": needs 1–4 usage summaries (tool, action, refs), not only tool names; group related operations")
                continue
            for usage_number, item in enumerate(usage):
                item_location = location + "/" + str(usage_number)
                if not isinstance(item, dict) or any(not isinstance(item.get(key), str) or not item[key].strip() for key in ("tool", "action")):
                    errors.append(item_location + ": needs a tool name and concrete action/target")
                    continue
                refs = item.get("refs")
                if not isinstance(refs, list) or not refs or any(not isinstance(ref, str) or ref not in step.get("tool_refs", []) for ref in refs):
                    errors.append(item_location + "/refs: must belong to this step's tool_refs and phase tool_refs. Supplied=" + str(refs) + "; selected step refs=" + str(step.get("tool_refs")))
                    continue
                names = {index.calls.get(ref, {}).get("tool") or index.events.get(ref, {}).get("tool") or "unknown" for ref in refs}
                if item["tool"] not in names:
                    errors.append(item_location + ": tool=" + item["tool"] + " does not match tools at supplied refs=" + str(refs) + "; actual names=" + str(sorted(str(name) for name in names)) + ". Correct the tool or its refs from source, then update step/phase tool_refs consistently.")
    return errors


def handoff_phase(phase, index):
    label = index.label
    lines = ["### " + phase["id"] + " — " + phase["title"], "", phase["summary"], ""]
    for number, step in enumerate(phase["tool_steps"], 1):
        lines.extend([str(number) + ". **" + step["purpose"] + "**", ""])
        for usage in step["usage"]:
            lines.extend(["   - **" + usage["tool"] + "** — " + usage["action"] + " (" + index.cite(usage["refs"]) + ")", ""])
        lines.extend(["   **" + label("Observed", "观察") + "**: " + step["finding"], "",
                      "   **" + label("Decision", "决策") + "**: " + step["decision"], ""])
    rationale = phase["rationale"]
    if rationale["basis"] != "not_recorded":
        lines.extend([label("Rationale", "依据") + " [" + rationale["basis"] + "]: " + rationale["text"], ""])
    lines.extend([label("Outcome", "结果") + " [" + phase["outcome"] + "]: " + phase["observation"], "",
                  label("Next state: ", "后续状态：") + phase["next_state"], "",
                  label("Selected evidence: ", "关键来源：") + index.cite([*phase["human_refs"], *phase["refs"], *rationale["refs"]]), ""])
    return lines
