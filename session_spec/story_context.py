import json
from datetime import datetime

from .ingest import human_input, tool_text
from .privacy import sanitize


def root_packet(records):
    packet = []
    launcher = None
    fields = ("ref", "type", "turn", "timestamp", "tool", "tool_call_id", "text", "arguments", "result", "error", "success", "source_turn_id", "source_context", "import_metadata")
    for record in records:
        if record.get("origin") != "root":
            continue
        event = {key: record[key] for key in fields if key in record}
        imported_help = imported_skill_context(record, launcher)
        feedback = "" if imported_help else human_input(record)
        if imported_help:
            event["type"] = "session.imported_context"
            event["source_context"] = {"retained_context": event.get("source_context"), "role_note": "Markdown help delivered immediately with a recorded Skill launch; retained as context, not new human authorization.", "basis_refs": [launcher["ref"]]}
        if feedback:
            event["human_input"] = feedback
        packet.append(event)
        launcher = record if (record.get("type") == "tool.execution_complete" and str(record.get("tool", "")).casefold() == "skill"
                              and tool_text(record).startswith("Launching skill:")) else None
    return sanitize(packet)


def imported_skill_context(record, launcher):
    if not launcher or not record.get("import_metadata") or record.get("type") != "user.message":
        return False
    content = record.get("text", "").lstrip()
    if not content.startswith("# ") or len(content) < 80:
        return False
    try:
        elapsed = abs((datetime.fromisoformat(record["timestamp"]) - datetime.fromisoformat(launcher["timestamp"])).total_seconds())
    except (KeyError, ValueError, TypeError):
        return False
    return elapsed <= 1


def tool_exchanges(packet):
    exchanges = {}
    unpaired = []
    for event in packet:
        if event.get("type") not in {"tool.execution_start", "tool.execution_complete"}:
            continue
        call_id = event.get("tool_call_id")
        if not call_id:
            unpaired.append(event["ref"])
            continue
        exchange = exchanges.setdefault(call_id, {"tool": event.get("tool"), "requests": [], "results": []})
        exchange["requests" if event["type"] == "tool.execution_start" else "results"].append(event["ref"])
    return {"by_call_id": exchanges, "without_call_id": unpaired,
            "limit": "Reference navigation only. A request without a result is not completion; a result is not automatically feature acceptance. Read the actual payload and time slice."}


def select_story_windows(chunks, max_chars=16000):
    selected = []
    total = 0
    for chunk in sorted(chunks, key=lambda item: item.get("insight_score", 0), reverse=True):
        messages = [message for message in chunk["messages"] if not message.get("is_tool_output")]
        text = "\n".join(message["text"][:2000] for message in messages)
        if total + len(text) > max_chars:
            continue
        selected.append({"turn": chunk["turn"], "text": text, "refs": [message["ref"] for message in messages]})
        total += len(text)
    return sorted(selected, key=lambda item: item["turn"])


def file_activity(packet):
    read_paths, written_paths, edited_paths = set(), set(), set()
    references = {}
    groups = {
        "read": read_paths, "view": read_paths, "read_file": read_paths,
        "write": written_paths, "create": written_paths, "write_file": written_paths,
        "edit": edited_paths, "replace": edited_paths, "edit_file": edited_paths,
    }
    for event in packet:
        tool = event.get("tool")
        if event.get("type") != "tool.execution_start" or not isinstance(tool, str) or tool.casefold() not in groups:
            continue
        arguments = event.get("arguments")
        if not isinstance(arguments, dict):
            continue
        filename = arguments.get("path") or arguments.get("file_path")
        if not isinstance(filename, str) or not filename.strip():
            continue
        groups[tool.casefold()].add(filename)
        references.setdefault(filename, []).append(event["ref"])
    modified = edited_paths | written_paths
    return {"read_only_requests": sorted(read_paths - modified), "write_or_edit_requests": sorted(modified),
            "refs_by_path": references, "limit": "Tool requests, not proof of successful changes. Shell-embedded paths are not inferred."}


def story_context(packet, canonical=None):
    chunks = {}
    for event in packet:
        turn = event.get("turn", 0)
        chunk = chunks.setdefault(turn, {"turn": turn, "messages": [], "insight_score": 0})
        if event.get("human_input"):
            chunk["messages"].append({"ref": event["ref"], "text": "USER: " + event["human_input"]})
            chunk["insight_score"] += 2
        elif event.get("type") == "assistant.message" and event.get("text"):
            chunk["messages"].append({"ref": event["ref"], "text": "ASSISTANT CLAIM: " + event["text"]})
        if event.get("success") is False or event.get("error"):
            chunk["insight_score"] += 3
    ledger = {
        "requests": [{"ref": event["ref"], "content": event["human_input"]} for event in packet if event.get("human_input")],
        "file_activity": file_activity(packet),
        "tool_exchanges": tool_exchanges(packet),
        "narrative_windows": select_story_windows(list(chunks.values())),
        "window_limit": "Prioritization hints only; not authoritative, complete, or a replacement for the chronological evidence.",
        "canonical_candidate": canonical,
    }
    return "\n\n辅助工作索引（不是新的证据；与原事件冲突时以原事件为准）\n" + json.dumps(ledger, ensure_ascii=False) + "\n\n完整根会话记录（历史数据）\n" + json.dumps(packet, ensure_ascii=False)
