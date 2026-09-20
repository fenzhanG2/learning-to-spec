import re


SCHEMA = "review-focus/v1"


def review_focus(edition):
    short_claims = []
    handoff_conditions = []

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

    for root in ("article", "insights"):
        visit(edition.get(root, {}), "/" + root)
    return {"schema": SCHEMA, "short_claims": short_claims, "handoff_conditions": handoff_conditions}
