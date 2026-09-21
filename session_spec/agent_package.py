from .agent_evidence import EvidenceIndex
from .agent_handoff import render_agent, tool_ledger
from .storage import write_json


LEGACY_PRESENTATION = {"schema": "agent-presentation/v1", "evidence_substitution": "literal"}
PRESENTATION = {"schema": "agent-presentation/v2", "mode": "markdown-files"}
EVIDENCE_RENDERER = "companion/v7"
HUMAN_PRESENTATION = {"schema": "human-presentation/v1", "source_heading": "neutral"}


def render_agent_package(article, events, language, trajectory_style=None):
    markdown = render_agent(article, events, language, trajectory_style)
    files = {"agent-spec.md": markdown}
    complete_short = trajectory_style in {"handoff-portable-v3", "handoff-portable-v4", "handoff-portable-v5"} or trajectory_style is None and article.get("agent_detail", {}).get("schema") == "agent-detail/v3"
    payload_aware = trajectory_style == "handoff-portable-v2" or complete_short
    portable = trajectory_style == "handoff-portable" or payload_aware
    separate = trajectory_style == "handoff-split" or portable
    if separate:
        files["evidence.md"] = EvidenceIndex(events, tool_ledger(events), language, portable=portable, payload_aware=payload_aware, complete_short=complete_short).companion(markdown)
    return files


def write_agent_package(article, events, language, directory, support):
    files = render_agent_package(article, events, language)
    for name, content in files.items():
        (directory / name).write_bytes(content.encode("utf-8"))
    (support / "agent-rendered.md").write_bytes(files["agent-spec.md"].encode("utf-8"))
    if "evidence.md" in files:
        (support / "evidence-rendered.md").write_bytes(files["evidence.md"].encode("utf-8"))
        write_json(support / "agent-presentation.json", PRESENTATION)
    return files
