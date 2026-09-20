import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from session_spec.private_cli import generate_private
from session_spec.reduction import recommended_decisions, scan_session


class PrivateResumeTests(unittest.TestCase):
    def test_resume_is_bound_to_source_and_exact_decisions(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source/events.jsonl"
            source.parent.mkdir()
            source.write_text(json.dumps({"type": "user.message", "data": {"content": "Contact fake@example.invalid. Keep the failed test."}}) + "\n", encoding="utf-8")
            review = scan_session(str(source), root / "home", root / "review", preferences={"readers": "both", "destination": "local"})
            choices = recommended_decisions(review)

            def fail(*args, **kwargs):
                marker = root / "output/story/_support/story-source.json"
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.write_text("{}")
                raise ValueError("Simulated bounded reviewer failure")

            with patch("session_spec.private_cli.run_story", side_effect=fail):
                with self.assertRaises(ValueError):
                    generate_private(root / "review", choices, root / "output", root / "home", confirm_choices=True)
            with patch("session_spec.private_cli.run_story", return_value={"status": "reviewed_draft"}) as run, patch("session_spec.private_cli.deliver", return_value={"files": []}):
                generate_private(root / "review", choices, root / "output", root / "home", resume=True, confirm_choices=True)
                self.assertTrue(run.call_args.kwargs["resume"])
                changed = {**choices, "choices": {key: {"action": "keep"} for key in choices["choices"]}}
                with self.assertRaisesRegex(ValueError, "changed"):
                    generate_private(root / "review", changed, root / "output", root / "home", resume=True, confirm_choices=True)


if __name__ == "__main__":
    unittest.main()
