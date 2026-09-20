import json
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
    def test_retry_rechecks_structural_diagnostics_and_retains_previous_cost(self):
        checked = [{"category": category, "note": "Checked"} for category in CHECKS]
        draft = {"article": {"title": "Draft", "route": []}, "brief": {}, "insights": {}}
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            with patch("session_spec.story_editor.validate_edition", return_value=["Old generic diagnostic"]), \
                 patch("session_spec.story_editor.validate_language", return_value=[]):
                with self.assertRaisesRegex(ValueError, "bounded review"):
                    generate_edition(directory, draft, [], Backend([]), lambda *args: [], max_repairs=0)
            attempt = json.loads((directory / "edition-attempt.json").read_bytes())
            self.assertEqual(list(attempt["failures"].values()), [[]])
            attempt["calls"] = [{"label": "prior-call", "elapsed_seconds": 12.5}]
            attempt["failures"][attempt["candidate_sha256"]] = [{"reason": "Old generic diagnostic"}]
            attempt.pop("failure_protocol")
            (directory / "edition-attempt.json").write_text(json.dumps(attempt), encoding="utf-8")
            backend = Backend([{"issues": [], "suggestions": [], "checked": checked}])
            with patch("session_spec.story_editor.validate_edition", return_value=[]), \
                 patch("session_spec.story_editor.validate_language", return_value=[]):
                self.assertEqual(generate_edition(directory, draft, [], backend, lambda *args: [], max_repairs=0), draft)
            receipt = json.loads((directory / "edition-receipt.json").read_bytes())
            self.assertEqual(receipt["previous_runs"][0]["calls"], attempt["calls"])
            self.assertEqual(backend.calls, [{"label": "story-edition-review"}])

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
