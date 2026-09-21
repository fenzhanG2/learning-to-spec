import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from offline_provider import guard_offline_test
from session_spec.abstract_privacy import generate_abstract_private, load_abstract_review, prepare_abstract_review, validate_abstract_story
from session_spec.backend import preparation_budget
from session_spec.fast_quality import QualityReviewFailure
from test_fast_quality import quality
from test_fast_story import draft
from test_story_pipeline import FakeBackend


@unittest.skipUnless(shutil.which("node"), "Node.js is required for deterministic rendering")
class FastAbstractPrivacyTests(unittest.TestCase):
    def setUp(self):
        guard_offline_test(self)
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source/events.jsonl"
        self.source.parent.mkdir()
        events = [{"type": "user.message", "data": {"content": "保留功能，只隐藏窗口。"}},
                  {"type": "tool.execution_complete", "data": {"toolName": "shell", "result": {"content": "Readback: task calls wrapper; startup signal observed."}, "success": True}}]
        self.source.write_text("".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events), encoding="utf-8")
        self.review_directory = self.root / "job/review"
        self.output = self.root / "job/generation-test"

    def tearDown(self):
        self.temporary.cleanup()

    def prepare(self, responses, mode="llm", model=None):
        self.backend = FakeBackend(responses)
        self.backend_settings = None

        def factory(**settings):
            self.backend_settings = settings
            return self.backend

        with preparation_budget(), patch("session_spec.story_pipeline.run_story", side_effect=AssertionError("No research pipeline")):
            return prepare_abstract_review(self.source, self.root / "home", self.review_directory, "local",
                                           {"readers": "both", "destination": "local"}, mode, fast=True,
                                           backend_factory=factory, model=model)

    def export(self, review):
        decisions = {"review_id": review["review_id"], "audience": "local", "choices": {
            finding["id"]: {"action": "keep"} for finding in review["findings"]}}
        return generate_abstract_private(self.review_directory, decisions, self.output, self.root / "home", confirm_choices=True)

    def test_two_calls_through_real_render_review_and_export_without_postapproval_model(self):
        review = self.prepare([draft(), {"quality": quality(), "findings": [], "limitations": []}])
        self.assertIsNone(self.backend_settings["model"])
        self.assertIsNone(self.backend_settings["auto_tier"])
        self.assertIsNone(self.backend_settings["reasoning_effort"])
        self.assertEqual(len(self.backend.calls), 2)
        manifest = load_abstract_review(self.review_directory, validate_parent=True)
        self.assertEqual(manifest["quality_profile"], "bounded-source-review/v1")
        result = self.export(review)
        self.assertEqual(len(self.backend.calls), 2)
        self.assertEqual(result["model_calls_after_approval"], 0)
        self.assertEqual(set(result["files"]), {"human-spec.html", "agent-spec.md", "evidence.md"})
        self.assertTrue(validate_abstract_story(self.output / "story")["valid"])

    def test_one_structural_repair_makes_exactly_three_total_calls(self):
        invalid = copy.deepcopy(draft())
        invalid["article"]["chapters"][0]["markdown"] += " E000002"
        repair = {"replacements": [{"path": ["article", "chapters", 0, "markdown"], "value": draft()["article"]["chapters"][0]["markdown"]}]}
        review = self.prepare([invalid, repair, {"quality": quality(), "findings": [], "limitations": []}])
        self.assertEqual([call["label"] for call in self.backend.calls], ["fast-story-draft", "fast-story-repair", "bounded-quality-privacy"])
        self.export(review)
        self.assertEqual(len(self.backend.calls), 3)

    def test_explicit_auto_preserves_user_choice_without_incompatible_effort_or_tier(self):
        self.prepare([draft(), {"quality": quality(), "findings": [], "limitations": []}], model="auto")
        self.assertEqual(self.backend_settings["model"], "auto")
        self.assertIsNone(self.backend_settings["reasoning_effort"])
        self.assertIsNone(self.backend_settings["auto_tier"])

    def test_explicit_named_model_is_not_replaced_or_given_assumed_reasoning_support(self):
        self.prepare([draft(), {"quality": quality(), "findings": [], "limitations": []}], model="synthetic-chosen-model")
        self.assertEqual(self.backend_settings["model"], "synthetic-chosen-model")
        self.assertIsNone(self.backend_settings["reasoning_effort"])
        self.assertIsNone(self.backend_settings["auto_tier"])

    def test_provider_refusal_does_not_trigger_model_substitution_or_structural_repair(self):
        with patch.object(FakeBackend, "generate", side_effect=ValueError("Selected model is not available")) as generate:
            with self.assertRaisesRegex(ValueError, "Selected model is not available"):
                self.prepare([], model="synthetic-unavailable-model")
        self.assertEqual(self.backend_settings["model"], "synthetic-unavailable-model")
        generate.assert_called_once()
        self.assertFalse(self.review_directory.exists())
        self.assertFalse((self.output / "deliverables").exists())

    def test_full_mode_still_gets_source_quality_but_no_privacy_review(self):
        with patch("session_spec.abstract_privacy.semantic_review", side_effect=AssertionError("No privacy scanning in full mode")):
            review = self.prepare([draft(), {"quality": quality(), "findings": [], "limitations": []}], "full")
        self.assertEqual(review["semantic"]["status"], "skipped_by_choice")
        self.assertEqual(len(self.backend.calls), 2)
        self.export(review)

    def test_failed_source_quality_never_allows_a_structural_only_export(self):
        result = quality()
        result.update(verdict="fail", issues=[{"reason": "The source requires retaining the original functionality.", "refs": ["E000001"], "quote": "保留功能"}])
        with self.assertRaises(QualityReviewFailure):
            self.prepare([draft(), {"quality": result, "findings": [], "limitations": []}])
        with self.assertRaises(ValueError):
            load_abstract_review(self.review_directory, validate_parent=True)
        self.assertFalse((self.output / "deliverables").exists())


if __name__ == "__main__":
    unittest.main()
