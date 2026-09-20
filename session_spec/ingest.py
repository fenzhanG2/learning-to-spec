import hashlib
import json
import os
import re
import sqlite3
from collections import Counter
from pathlib import Path

from .privacy import excerpt, sanitize, text_of


KEPT_TYPES = {
    "user.message", "assistant.message", "tool.execution_start",
    "tool.execution_complete", "session.task_complete", "session.compaction_complete",
    "subagent.started", "subagent.completed",
    "session.imported_context",
}


def copilot_home():
    return Path(os.environ.get("COPILOT_HOME", str(Path.home() / ".copilot"))).expanduser()


def metadata_rows(home):
    database = home / "data.db"
    if not database.exists():
        return {}
    connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        connection.row_factory = sqlite3.Row
        columns = {row[1] for row in connection.execute("PRAGMA table_info(sessions)")}
        wanted = [name for name in ("id", "title", "updated_at", "is_running") if name in columns]
        if "id" not in wanted:
            return {}
        return {row["id"]: sanitize(dict(row)) for row in connection.execute("SELECT " + ",".join(wanted) + " FROM sessions")}
    except sqlite3.DatabaseError:
        return {}
    finally:
        connection.close()


def list_sessions(home, limit=30, query=""):
    metadata = metadata_rows(home)
    result = []
    for path in (home / "session-state").glob("*/events.jsonl"):
        row = metadata.get(path.parent.name, {})
        title = row.get("title") or path.parent.name
        if title.startswith("session-spec-"):
            continue
        if query and query.casefold() not in (title + " " + path.parent.name).casefold():
            continue
        result.append({
            "id": path.parent.name, "title": title, "path": str(path),
            "bytes": path.stat().st_size, "modified": path.stat().st_mtime,
            "running": bool(row.get("is_running", False)),
        })
    return sorted(result, key=lambda item: item["modified"], reverse=True)[:limit]


def resolve_session(value, home):
    candidate = Path(value).expanduser()
    if candidate.is_dir():
        candidate = candidate / "events.jsonl"
    if candidate.is_file():
        return candidate.resolve()
    if not re.fullmatch(r"[a-fA-F0-9-]{7,36}", value):
        raise ValueError("Expected a session UUID, unique UUID prefix, or events.jsonl path.")
    matches = list((home / "session-state").glob(value + "*/events.jsonl"))
    if len(matches) != 1:
        raise ValueError(f"Session selector matches {len(matches)} sessions; use an exact UUID.")
    return matches[0].resolve()


def origin_of(event, session_id):
    data = event.get("data") or {}
    source = str(data.get("source") or "")
    content = str(data.get("content") or "").lstrip()
    agent_id = data.get("agentId") or event.get("agentId")
    if source.startswith("agent-") or data.get("parentToolCallId") or (agent_id and agent_id != session_id):
        return "delegated"
    if source.startswith("skill-") or source in {"system", "harness"} or content.startswith("<canvas-context>"):
        return "injected"
    if source and source not in {"user", "human"}:
        return "unknown"
    return "root"


def read_session(path, home):
    session_id = path.parent.name
    metadata = metadata_rows(home).get(session_id, {})
    initial_size = path.stat().st_size
    digest = hashlib.sha256()
    records = []
    counts = Counter()
    warnings = []
    tool_names = {}
    turn = 0
    consumed = 0
    with path.open("rb") as stream:
        line_number = 0
        while consumed < initial_size:
            raw = stream.readline(initial_size - consumed)
            if not raw:
                break
            line_number += 1
            consumed += len(raw)
            digest.update(raw)
            if not raw.strip():
                continue
            try:
                event = json.loads(raw.decode("utf-8-sig"))
            except (ValueError, UnicodeDecodeError):
                if consumed == initial_size and not raw.endswith(b"\n"):
                    warnings.append(f"Ignored incomplete final event at line {line_number}.")
                    continue
                raise ValueError(f"Invalid event JSON at line {line_number}; refusing silent data loss.") from None
            if not isinstance(event, dict) or not isinstance(event.get("data", {}), dict):
                raise ValueError(f"Invalid event object at line {line_number}.")
            kind = event.get("type", "unknown")
            counts[kind] += 1
            data = event.get("data") or {}
            if kind == "session.start":
                metadata["context"] = sanitize(data.get("context", {}))
                session_id = data.get("sessionId") or session_id
            if kind not in KEPT_TYPES:
                continue
            origin = origin_of(event, session_id)
            if kind == "user.message" and origin == "root":
                turn += 1
            record = {
                "ref": f"E{line_number:06}", "line": line_number,
                "event_id": event.get("id"), "timestamp": event.get("timestamp"),
                "type": kind, "origin": origin, "turn": turn,
                "text": text_of(data.get("content")),
            }
            for source_key, record_key in (("sourceTurnId", "source_turn_id"), ("sourceContext", "source_context"), ("importMetadata", "import_metadata")):
                if source_key in data:
                    record[record_key] = sanitize(data[source_key])
            if kind.startswith("tool."):
                call_id = data.get("toolCallId", "")
                name = data.get("toolName") or tool_names.get(call_id, "unknown")
                if data.get("toolName"):
                    tool_names[call_id] = name
                record.update({"tool_call_id": call_id, "tool": name})
                if "arguments" in data:
                    record["arguments"] = sanitize(data["arguments"])
                if "result" in data:
                    record["result"] = sanitize(data["result"])
                if "success" in data:
                    record["success"] = data["success"]
                if "error" in data:
                    record["error"] = sanitize(data["error"])
            if kind not in {"user.message", "assistant.message"} and not kind.startswith("tool."):
                record["details"] = sanitize({key: data[key] for key in ("summary", "agentId", "toolCallId", "success", "error") if key in data})
            if automated_feedback(record):
                record["feedback_origin"] = "automated_control"
            records.append(record)
    if path.stat().st_size != initial_size:
        warnings.append("Source changed during capture; this export is a bounded prefix snapshot.")
    if not turn:
        raise ValueError("No top-level user messages found in this Copilot session.")
    metadata.update({
        "id": session_id, "title": metadata.get("title") or session_id,
        "source_path": str(path), "source_sha256": digest.hexdigest(),
        "snapshot_bytes": initial_size, "event_counts": dict(counts),
        "root_user_turns": turn, "retained_events": len(records),
        "warnings": warnings, "hidden_reasoning_exported": False,
    })
    return metadata, records


def tool_text(record):
    payload = record.get("result", record.get("arguments", record.get("error", "")))
    if isinstance(payload, dict):
        for key in ("content", "text", "output", "message"):
            if key in payload:
                return text_of(payload[key])
    return text_of(payload)


def automated_feedback(record):
    if record.get("type") != "tool.execution_complete" or record.get("tool") != "ask_user":
        return False
    content = " ".join(tool_text(record).split()).casefold()
    return content == "user responded: the user is not available to respond and will review your work later. work autonomously and make good decisions."


def human_input(record):
    if record["origin"] != "root":
        return ""
    if record["type"] == "user.message":
        return record["text"]
    if record["type"] == "tool.execution_complete" and record.get("tool") == "ask_user" and record.get("success") is True:
        if automated_feedback(record):
            return ""
        content = tool_text(record)
        for prefix in ("User selected:", "User answered:", "User responded:"):
            if content.startswith(prefix):
                return content[len(prefix):].strip()
    return ""


def make_digest(records, tool_limit=8, message_limit=2400):
    turns = {}
    for record in records:
        turns.setdefault(record["turn"], []).append(record)
    digest = []
    root_refs = []
    feedback_refs = []
    control_refs = []
    selected_refs = set()
    omitted_chars = 0
    for turn, entries in turns.items():
        tools = [record for record in entries if record["type"].startswith("tool.")]
        failures = [record for record in tools if record.get("success") is False]
        candidates = failures + list(reversed(tools))
        selected_tools = set()
        seen_tools = set()
        for record in candidates:
            identity = (record.get("tool"), record["type"], record.get("success"))
            if identity not in seen_tools or len(selected_tools) < tool_limit // 2:
                selected_tools.add(record["ref"])
                seen_tools.add(identity)
            if len(selected_tools) >= tool_limit:
                break
        dialogue = [record for record in entries if record["origin"] == "root" and record["type"] in {"user.message", "assistant.message"} and record["text"]]
        selected_tools.update(record["ref"] for record in tools if record.get("tool") == "ask_user" and record["origin"] == "root")
        pieces = []
        for record in sorted(dialogue + [record for record in tools if record["ref"] in selected_tools], key=lambda item: item["line"]):
            selected_refs.add(record["ref"])
            if record["type"] == "user.message":
                root_refs.append(record["ref"])
                content = record["text"]
            elif human_input(record):
                feedback_refs.append(record["ref"])
                content = "EXPLICIT HUMAN FEEDBACK via ask_user: " + human_input(record)
            elif automated_feedback(record):
                control_refs.append(record["ref"])
                content = "AUTOMATED CONTROL RESPONSE, NOT HUMAN AUTHORIZATION: " + tool_text(record)
            elif record["type"] == "assistant.message":
                content = excerpt(record["text"], message_limit)
                omitted_chars += max(0, len(record["text"]) - message_limit)
            else:
                content = excerpt(tool_text(record), 900)
                omitted_chars += max(0, len(tool_text(record)) - 900)
            pieces.append({
                "ref": record["ref"], "type": "user.feedback" if record["ref"] in feedback_refs else "control.feedback" if record["ref"] in control_refs else record["type"], "origin": record["origin"],
                "tool": record.get("tool"), "success": record.get("success"), "content": content,
            })
        digest.append({
            "turn": turn, "events": pieces,
            "tool_index": [
                f"{record['ref']} {record['tool']} {'start' if record['type'].endswith('start') else 'ok' if record.get('success') is True else 'failed' if record.get('success') is False else 'unknown'} {record['origin']}"
                for record in tools
            ],
            "delegated_or_injected_messages": sum(record["origin"] != "root" and record["type"] in {"user.message", "assistant.message"} for record in entries),
        })
    return digest, {
        "root_request_refs": root_refs, "human_feedback_refs": feedback_refs, "control_feedback_refs": control_refs, "selected_event_refs": sorted(selected_refs),
        "tool_excerpts_per_turn": tool_limit, "excerpt_omitted_chars": omitted_chars,
        "unselected_events": len(records) - len(selected_refs),
        "policy": "All root requests and nonempty root assistant messages included; tool metadata indexed; payload excerpts are selective. Known automated ask_user-unavailable fallbacks are retained as control data, not human authorization. Full sanitized observable events remain in evidence.jsonl. No semantic-completeness guarantee.",
    }


def chunk_digest(digest, budget=80000):
    chunks = []
    current = []
    size = 0
    for turn in digest:
        pieces = [turn]
        encoded = json.dumps(turn, ensure_ascii=False)
        if len(encoded) > budget:
            pieces = []
            for event in turn["events"]:
                content = event["content"]
                for offset in range(0, max(1, len(content)), budget // 2):
                    fragment = dict(event, content=content[offset:offset + budget // 2])
                    pieces.append({"turn": turn["turn"], "partial_turn": True, "events": [fragment]})
            for offset in range(0, len(turn["tool_index"]), 100):
                pieces.append({"turn": turn["turn"], "partial_turn": True, "events": [], "tool_index": turn["tool_index"][offset:offset + 100]})
        for piece in pieces:
            length = len(json.dumps(piece, ensure_ascii=False))
            if current and size + length > budget:
                chunks.append(current)
                current, size = [], 0
            current.append(piece)
            size += length
    if current:
        chunks.append(current)
    return chunks
