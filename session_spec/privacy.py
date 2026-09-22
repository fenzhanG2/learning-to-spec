import json
import re
from contextlib import contextmanager
from contextvars import ContextVar


_CONTENT_REDACTION = ContextVar("content_redaction", default=True)


def redaction_enabled():
    return _CONTENT_REDACTION.get()


@contextmanager
def content_redaction(enabled=True):
    if not isinstance(enabled, bool):
        raise ValueError("Explicit content redaction mode required")
    token = _CONTENT_REDACTION.set(enabled)
    try:
        yield
    finally:
        _CONTENT_REDACTION.reset(token)


HIDDEN_FIELDS = {
    "reasoningText", "reasoningOpaque", "encryptedContent", "thinking",
    "reasoning_content", "signature", "base64", "imageData",
}
SECRET_FIELD = re.compile(
    r"^(?:[A-Z0-9]+[_ -])*(?:password|passwd|pwd|secret|client[_ -]?secret|api[_ -]?key|"
    r"access[_ -]?token|refresh[_ -]?token|authorization|cookie|"
    r"accountkey|account[_ -]?key|private[_ -]?key|secret[_ -]?key|secret[_ -]?access[_ -]?key|session[_ -]?token)$", re.I,
)
SECRET_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,}|GOCSPX-[A-Za-z0-9_-]+)\b"),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"(?<=://)[^\s/@:]*:[^\s/@]+(?=@)"),
    re.compile(r"(?im)^[ \t]*(?:Set-Cookie|Cookie):[ \t]*[^\r\n]+"),
    re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
    re.compile(r"(?i)(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{12,}"),
    re.compile(r"(?i)(?<=sig=)[A-Za-z0-9%+/_=-]{12,}"),
    re.compile(r"(?im)\b(?:[A-Z][A-Z0-9]*[_-])*(?:client[_ -]?secret|access[_ -]?token|refresh[_ -]?token|api[_ -]?key|password|passwd|accountkey|secret)\s*[`\"']?\s*[:=]\s*[`\"']?[^\s`\"',;}]{6,}"),
    re.compile(r"(?i)([A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\s*/\s*)\S+"),
    re.compile(r"data:[^;\s,]+(?:;charset=[^;\s,]+)?;base64,[A-Za-z0-9+/=]+", re.I),
]


def visible_text(value):
    text = str(value or "")
    return re.sub(r'"(?:reasoningText|reasoningOpaque|encryptedContent|thinking|reasoning_content)"\s*:\s*"(?:\\.|[^"\\])*"', '"hidden_field_omitted": true', text)


def redact_text(value):
    text = visible_text(value)
    for pattern in SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text


def sanitize(value):
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]" if redaction_enabled() and SECRET_FIELD.fullmatch(str(key)) else sanitize(content)
            for key, content in value.items() if key not in HIDDEN_FIELDS
        }
    if isinstance(value, list):
        return [sanitize(content) for content in value]
    if isinstance(value, str):
        return redact_text(value) if redaction_enabled() else visible_text(value)
    return value


def text_of(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return sanitize(value)
    return json.dumps(sanitize(value), ensure_ascii=False)


def excerpt(text, limit):
    if len(text) <= limit:
        return text
    head = limit * 2 // 3
    return text[:head] + f"\n[OMITTED {len(text) - limit} CHARS; expand source ref]\n" + text[-(limit - head):]
