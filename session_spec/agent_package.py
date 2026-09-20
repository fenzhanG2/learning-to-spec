from .agent_evidence import EvidenceIndex
from .agent_handoff import render_agent, tool_ledger
from .storage import write_json


LEGACY_PRESENTATION = {"schema": "agent-presentation/v1", "evidence_substitution": "literal"}
PRESENTATION = {"schema": "agent-presentation/v2", "mode": "markdown-files"}


def render_agent_package(article, events, language, trajectory_style=None):
    markdown = render_agent(article, events, language, trajectory_style)
    files = {"agent-spec.md": markdown}
    portable = trajectory_style == "handoff-portable" or trajectory_style is None and article.get("agent_detail", {}).get("schema") == "agent-detail/v3"
    separate = trajectory_style == "handoff-split" or portable
    if separate:
        files["evidence.md"] = EvidenceIndex(events, tool_ledger(events), language, portable=portable).companion(markdown)
    return files


def write_agent_package(article, events, language, directory, support):
    files = render_agent_package(article, events, language)
    for name, content in files.items():
        (directory / name).write_text(content, encoding="utf-8")
    (support / "agent-rendered.md").write_text(files["agent-spec.md"], encoding="utf-8")
    if "evidence.md" in files:
        (support / "evidence-rendered.md").write_text(files["evidence.md"], encoding="utf-8")
        write_json(support / "agent-presentation.json", PRESENTATION)
    return files
