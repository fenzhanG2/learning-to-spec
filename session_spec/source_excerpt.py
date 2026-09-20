import json
import re


MAX_SOURCE_CHARS = 6000


def source_payload(event):
    kind = event.get("type", "")
    if kind == "tool.execution_start":
        return event.get("arguments")
    if kind == "tool.execution_complete":
        payload = {key: event[key] for key in ("result", "error") if key in event}
        return payload or event.get("text")
    if event.get("human_input"):
        return event["human_input"]
    return event.get("text")


def payload_text(value):
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, indent=2) if value is not None else ""


def excerpt_segments(text, limit=MAX_SOURCE_CHARS):
    if len(text) <= limit:
        return [{"start": 0, "end": len(text), "text": text}]
    head = limit * 3 // 4
    tail = limit - head
    return [{"start": 0, "end": head, "text": text[:head]},
            {"start": len(text) - tail, "end": len(text), "text": text[-tail:]}]


def source_excerpt(event):
    text = payload_text(source_payload(event))
    return {"ref": event["ref"], "type": event.get("type", "unknown"),
            "human_authority": bool(event.get("human_input")), "tool": event.get("tool"),
            "success": event.get("success"), "characters": len(text),
            "truncated": len(text) > MAX_SOURCE_CHARS, "segments": excerpt_segments(text)}


def fenced_text(text):
    fence = "`" * max(3, 1 + max((len(match) for match in re.findall(r"`+", text)), default=0))
    return fence + "text\n" + text + "\n" + fence
