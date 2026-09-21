from contextlib import contextmanager
from unittest.mock import patch

from offline_provider import forbid_live_provider
from session_spec.storage import write_json


def synthetic_story(session, home, output, **settings):
    content = (settings["from_export"] / "evidence.jsonl").read_text(encoding="utf-8")
    support = output / "_support"
    support.mkdir(parents=True)
    write_json(support / "edition.json", {"article": {"opening": content, "agent_markdown": content}, "brief": {}, "insights": []})
    write_json(support / "story-report.json", {"status": "synthetic-mock-not-model-output"})
    write_json(support / "language.json", {"language": "en"})
    for name in ("agent-spec.md", "evidence.md"):
        (output / name).write_bytes(content.encode("utf-8"))
    return {"status": "synthetic-mock-not-model-output"}


@contextmanager
def mocked_abstraction():
    """Mock abstraction infrastructure, not the fast generation or quality-review path."""
    from session_spec.abstract_privacy import prepare_abstract_review

    def infrastructure_review(*args, **settings):
        settings["fast"] = False
        return prepare_abstract_review(*args, **settings)

    with forbid_live_provider(), \
            patch("session_spec.native_bridge.prepare_abstract_review", side_effect=infrastructure_review), \
            patch("session_spec.story_pipeline.run_story", side_effect=synthetic_story) as drafting, \
            patch("session_spec.story_pipeline.validate_story", return_value={"valid": True, "issues": []}):
        yield drafting
