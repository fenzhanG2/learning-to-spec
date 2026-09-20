import re

from .story_grounding import pointer_value


SCHEMA = "review-focus/v5"


def comparison_groups(edition):
    article = edition.get("article", {})
    detail = article.get("agent_detail", {})
    commission = ["/brief/goals", "/brief/constraints", "/article/agent_markdown"]
    resumption = ["/article/agent_detail/resume/next_action", "/article/agent_detail/resume/workspace",
                  "/article/agent_detail/resume/verification_boundary", "/insights/closing/paragraphs"]
    resumption.extend(f"/article/chapters/{index}/markdown" for index in range(len(article.get("chapters", []))))
    for index, route in enumerate(detail.get("continuation", [])):
        base = f"/article/agent_detail/continuation/{index}"
        commission.extend(base + "/" + field for field in ("basis", "trigger", "done_when", "stop_when", "refs"))
        resumption.extend(base + "/" + field for field in ("trigger", "done_when", "stop_when"))
        if route.get("steps"):
            resumption.extend(base + "/steps/0/" + field for field in ("kind", "action", "precondition", "expected", "otherwise"))
    relevance = ["/article/human_input_coverage"]
    decisions = []
    for index, phase in enumerate(detail.get("trajectory", [])):
        base = f"/article/agent_detail/trajectory/{index}"
        relevance.extend(base + "/" + field for field in ("summary", "human_refs", "refs"))
        for step in range(len(phase.get("tool_steps", []))):
            decisions.extend(base + f"/tool_steps/{step}/" + field for field in ("finding", "decision", "refs"))
    groups = []
    for check, category, paths in (("commission_vs_design", "acceptance_scope", commission),
                                   ("first_move_and_human_continuation", "agent_handoff", resumption),
                                   ("accounting_vs_narrative", "narrative_and_scope", relevance),
                                   ("observed_vs_inferred_decisions", "evidence_strength", decisions)):
        present, missing = [], []
        for path in paths:
            try:
                pointer_value(edition, path)
            except ValueError:
                missing.append(path)
            else:
                present.append(path)
        if present:
            groups.append({"check": check, "category": category, "paths": present, "missing_paths": missing})
    return groups


def review_focus(edition):
    short_claims = []
    handoff_conditions = []
    mechanism_summaries = []

    def visit(value, path):
        if isinstance(value, dict):
            for key, child in value.items():
                visit(child, path + "/" + key.replace("~", "~0").replace("/", "~1"))
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, path + "/" + str(index))
        elif isinstance(value, str):
            field = path.rsplit("/", 1)[-1]
            if field in {"title", "subtitle", "period", "label"}:
                short_claims.append({"path": path, "quote": value})
            if field in {"markdown", "agent_markdown"}:
                for heading in re.findall(r"^#{1,6}[ \t]+[^\r\n]+", value, re.MULTILINE):
                    short_claims.append({"path": path, "quote": heading})
            if (path.startswith("/article/agent_detail/resume/")
                    or path.startswith("/article/agent_detail/continuation/")
                    and field in {"trigger", "action", "precondition", "expected", "otherwise", "done_when", "stop_when"}
                    or path.startswith("/article/agent_detail/paths/") and field == "reuse_condition"):
                handoff_conditions.append({"path": path, "quote": value})
            if (path == "/article/agent_markdown"
                    or path.startswith("/brief/") and field in {"text", "kind"}
                    or path.startswith("/insights/closing/paragraphs/")
                    or path.startswith("/article/agent_detail/recipes/")
                    and (field in {"when", "adapt", "avoid", "verify"} or "/procedure/" in path)
                    or path.startswith("/article/agent_detail/trajectory/")
                    and field in {"summary", "purpose", "action", "finding", "decision", "text", "observation", "next_state"}
                    or path.startswith("/article/agent_detail/paths/") and field == "reason"
                    or path.startswith("/article/checks/") and field in {"observed", "limit"}
                    or path.startswith("/insights/architecture/") and field in {"scope", "detail", "label", "evidence_summary", "limits"}):
                mechanism_summaries.append({"path": path, "quote": value})

    for root in ("article", "insights", "brief"):
        visit(edition.get(root, {}), "/" + root)
    return {"schema": SCHEMA, "short_claims": short_claims, "handoff_conditions": handoff_conditions,
            "mechanism_summaries": mechanism_summaries, "comparison_groups": comparison_groups(edition)}
