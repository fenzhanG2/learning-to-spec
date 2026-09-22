import copy
import io
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from session_spec.privacy_presentation import present_finding, present_review
from session_spec.private_cli import run


class PrivacyPresentationTests(unittest.TestCase):
    def finding(self):
        return {"id": "Psynthetic", "category": "inference", "label": "UNSUPPORTED_CONCLUSION",
                "text": "Invented exact selected source", "reason": "UNSUPPORTED_CONCLUSION",
                "alternative": "UNSUPPORTED_REWRITE", "necessity": "uncertain", "recommended": None,
                "detectors": ["copilot"], "occurrences": [{"path": [0, "data", "content"], "start": 0, "end": 30}],
                "related": ["S2"], "assessments": [{"reason": "UNSUPPORTED_CONCLUSION"}],
                "related_contexts": [{"event": 2, "field": ["data", "content"], "text": "Invented exact related source"}]}

    def test_model_hypotheses_are_not_presented_as_personal_facts_or_prefilled_rewrites(self):
        original = self.finding()
        before = copy.deepcopy(original)
        shown = present_finding(original)
        for canary in ("UNSUPPORTED_CONCLUSION", "UNSUPPORTED_REWRITE"):
            self.assertNotIn(canary, json.dumps(shown))
        self.assertIn("not a fact", shown["reason"])
        for key in ("id", "text", "occurrences", "related", "related_contexts", "necessity"):
            self.assertEqual(shown[key], original[key])
        self.assertEqual(shown["alternative"], "")
        self.assertEqual(original, before)

    def test_review_projection_retains_approval_identity_without_rewriting_the_audit(self):
        review = {"review_id": "bound-private-review", "findings": [self.finding()],
                  "semantic": {"status": "reviewed", "calls": 2, "limitations": ["UNSUPPORTED_LIMITATION"]}}
        original = copy.deepcopy(review)
        shown = present_review(review)
        self.assertEqual(shown["review_id"], review["review_id"])
        self.assertEqual(shown["semantic"]["calls"], 2)
        self.assertNotIn("UNSUPPORTED_", json.dumps(shown))
        self.assertEqual(review, original)

    def test_local_user_selected_redactions_keep_their_explicit_explanation(self):
        local = self.finding()
        local.update(detectors=["local"], category="custom", reason="You selected this exact phrase.", alternative="")
        shown = present_finding(local)
        self.assertEqual(shown, local)
        self.assertIsNot(shown, local)

    def test_cli_uses_bounded_questions_but_saves_the_original_choice_identity(self):
        review = {"review_id": "original-review", "audience": "local", "findings": [self.finding()]}
        arguments = SimpleNamespace(command="privacy-choose", directory=Path("synthetic-review"), out=Path("synthetic-choices.json"), recommended=False)
        capture = io.StringIO()
        baseline = [{"data": {"content": review["findings"][0]["text"]}}, {"data": {"content": "RELATED_CLUE_SHOWN"}}]
        before = copy.deepcopy(review)
        with patch("session_spec.private_cli.load_review", return_value=(review, baseline)), patch("builtins.input", return_value="keep"), \
                patch("session_spec.private_cli.write_json") as save, patch("sys.stdout", capture):
            run(arguments, {})
        self.assertNotIn("UNSUPPORTED_", capture.getvalue())
        self.assertIn(review["findings"][0]["text"], capture.getvalue())
        self.assertIn("RELATED_CLUE_SHOWN", capture.getvalue())
        self.assertIn('Event 2 · ["data", "content"]', capture.getvalue())
        self.assertEqual(review, before)
        self.assertEqual(save.call_args.args[1], {"review_id": "original-review", "audience": "local", "choices": {"Psynthetic": {"action": "keep"}}})

    def test_cli_shows_entire_selected_span(self):
        finding = self.finding()
        finding["text"] = "Synthetic source " * 40 + "VISIBLE_TAIL"
        finding["occurrences"][0]["end"] = len(finding["text"])
        review = {"review_id": "long-review", "audience": "local", "findings": [finding]}
        baseline = [{"data": {"content": finding["text"]}}]
        arguments = SimpleNamespace(command="privacy-choose", directory=Path("synthetic-review"), out=Path("synthetic-choices.json"), recommended=False)
        capture = io.StringIO()
        with patch("session_spec.private_cli.load_review", return_value=(review, baseline)), patch("builtins.input", return_value="keep"), \
                patch("session_spec.private_cli.write_json"), patch("sys.stdout", capture):
            run(arguments, {})
        self.assertIn(finding["text"], capture.getvalue())

    def test_assessment_notices_distinguish_disagreement_uncertainty_and_missingness(self):
        cases = [(None, "unavailable"), ([], "unavailable"), ([{"necessity": "uncertain"}], "uncertain"),
                 ([{"necessity": "necessary"}], "agreement"),
                 ([{"necessity": "necessary"}, {"necessity": "unnecessary"}], "disagreement"),
                 ([{"necessity": "unknown", "reason": "UNSUPPORTED_DETAIL"}], "invalid"), ("invalid", "invalid"),
                 ([{"necessity": []}], "invalid"), ([{"necessity": {}}], "invalid")]
        for assessments, status in cases:
            with self.subTest(status=status, assessments=assessments):
                finding = self.finding()
                finding["assessments"] = assessments
                shown = present_finding(finding)
                self.assertEqual(shown["assessment_summary"]["status"], status)
                self.assertNotIn("UNSUPPORTED_", json.dumps(shown))

    def test_scoped_projection_retains_all_categories_without_raw_judgments(self):
        finding = self.finding()
        finding.update(scope={"schema": "baseline-field/v2", "field_sha256": "synthetic"}, categories=["inference", "health"],
                       local_judgments=[{"reason": "UNSUPPORTED_LOCAL_DETAIL"}])
        shown = present_finding(finding)
        self.assertEqual(shown["categories"], ["health", "inference"])
        self.assertIn("medical information", shown["reason"])
        self.assertIn("linkable", shown["reason"])
        self.assertNotIn("UNSUPPORTED_", json.dumps(shown))
        finding["detectors"] = ["local"]
        self.assertNotIn("local_judgments", present_finding(finding))


if __name__ == "__main__":
    unittest.main()
