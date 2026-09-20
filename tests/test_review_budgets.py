import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from session_spec.story_editor import CHECKS, generate_edition


class Backend:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = []

    def generate(self, prompt, label):
        self.calls.append({"label": label})
        return next(self.replies)


class ReviewBudgetTests(unittest.TestCase):
    def test_format_repairs_do_not_consume_the_editorial_budget(self):
        checked = [{"category": category, "note": "Checked"} for category in CHECKS]
        issue = {"reason": "A substantive correction remains"}
        backend = Backend([
            {"patches": [{"op": "replace", "path": "/article/title", "value": "Shape fixed"}]},
            {"issues": [issue], "checked": checked},
            {"patches": [{"op": "replace", "path": "/article/title", "value": "Meaning fixed"}]},
            {"issues": [], "checked": checked},
        ])
        draft = {"article": {"title": "Draft", "route": []}, "brief": {}, "insights": {}}
        with tempfile.TemporaryDirectory() as temporary:
            with patch("session_spec.story_editor.validate_edition", side_effect=[["Bad schema"], [], []]), \
                 patch("session_spec.story_editor.validate_language", return_value=[]), \
                 patch("session_spec.story_editor.validate_grounding", return_value=[]), \
                 patch("session_spec.story_editor.resolve_review_locations", side_effect=lambda review, *args: (review, [])), \
                 patch("session_spec.story_editor.complete_reference_pairs", side_effect=lambda edition, events: (edition, [])):
                edition = generate_edition(Path(temporary), draft, [], backend, lambda *args: [], max_repairs=1, max_structural_repairs=1)
        self.assertEqual(edition["article"]["title"], "Meaning fixed")
        self.assertEqual(len(backend.calls), 4)


if __name__ == "__main__":
    unittest.main()
