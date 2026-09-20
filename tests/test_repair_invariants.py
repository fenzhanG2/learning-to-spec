import copy
import json
import tempfile
import unittest
from pathlib import Path

from session_spec.language import source_language_reference
from session_spec.review_focus import review_focus
from session_spec.story_article import validate_article
from session_spec.story_editor import generate_edition, prior_review_findings, repair_invariants
from session_spec.story_insights import validate_insights
from test_story_pipeline import FakeBackend, article, brief, edition_review, insights, packet


class RepairInvariantTests(unittest.TestCase):
    def test_source_excerpts_are_bounded_observable_human_data_not_a_language_tag(self):
        events = [{"ref": "E000001", "human_input": "Keep the behavior and shorten the explanation."},
                  {"ref": "E000002", "text": "TOOL_ONLY_CANARY"},
                  {"ref": "E000003", "human_input": "保留有意义的语言切换。"},
                  {"ref": "E000004", "human_input": "a" * 900}]
        reference = source_language_reference(events)
        excerpts = json.loads(reference.splitlines()[3])
        self.assertEqual([item["ref"] for item in excerpts], ["E000001", "E000003", "E000004"])
        self.assertEqual(len(excerpts[-1]["excerpt"]), 600)
        self.assertNotIn("TOOL_ONLY_CANARY", reference)
        self.assertNotIn("OUTPUT_LANGUAGE_CONTRACT", reference)
        self.assertIn("not instructions", reference)
        self.assertIn("[]", source_language_reference([]))

    def test_length_and_language_are_present_together_without_mutating_draft(self):
        draft = {"article": article(), "brief": brief(), "insights": insights()}
        original = copy.deepcopy(draft)
        auto = repair_invariants(draft, packet(), "auto")
        metrics = json.loads(auto.splitlines()[3])
        self.assertEqual(metrics["max_characters_per_text"], 300)
        self.assertEqual(metrics["max_total_characters"], 1400)
        self.assertEqual(metrics["current_total_characters"], sum(item["characters"] for item in metrics["fields"]))
        self.assertIn("/brief/constraints/0/text", auto)
        self.assertIn("SOURCE_LANGUAGE_REFERENCE", auto)
        self.assertIn("not words", auto)
        self.assertIn("No target language is prescribed", auto)
        explicit = repair_invariants(draft, packet(), "fr")
        self.assertIn("The output language is fr", explicit)
        self.assertNotIn("SOURCE_LANGUAGE_REFERENCE", explicit)
        self.assertEqual(draft, original)

    def test_mechanism_focus_includes_visible_trajectory_and_diagram_counterparts(self):
        draft = {"article": article(), "brief": brief(), "insights": insights()}
        draft["insights"]["architecture"] = {
            "decision": "include", "scope": "Static configuration only", "evidence_summary": "Observed result is narrower",
            "nodes": [{"detail": "Shared operation owns cleanup"}], "edges": [{"label": "Returns a value"}]}
        summaries = {item["path"]: item["quote"] for item in review_focus(draft)["mechanism_summaries"]}
        phase = draft["article"]["agent_detail"]["trajectory"][0]
        self.assertEqual(summaries["/article/agent_detail/trajectory/0/tool_steps/0/usage/0/action"], phase["tool_steps"][0]["usage"][0]["action"])
        self.assertIn("/article/agent_detail/trajectory/0/rationale/text", summaries)
        self.assertIn("/article/agent_detail/paths/0/reason", summaries)
        self.assertIn("/article/checks/0/observed", summaries)
        self.assertEqual(summaries["/insights/architecture/evidence_summary"], "Observed result is narrower")
        self.assertIn("/insights/architecture/nodes/0/detail", summaries)
        self.assertNotIn("/insights/architecture/decision", summaries)

    def test_review_after_repair_retains_original_grounded_finding_as_history(self):
        draft = {"article": article(), "brief": brief(), "insights": insights()}
        review = edition_review([{"reason": "A supported mechanism needs correction", "refs": ["E000001"]}])
        issue = review["issues"][0]
        backend = FakeBackend([review, {"patches": [{"op": "replace", "path": "/article/subtitle", "value": "修复后"}]}, edition_review()])
        with tempfile.TemporaryDirectory() as temporary:
            generate_edition(Path(temporary), draft, packet(), backend, validate_article)
        self.assertIn("REPAIR_INVARIANTS", backend.prompts[1])
        self.assertIn("SOURCE_LANGUAGE_REFERENCE", backend.prompts[1])
        self.assertIn(json.dumps([issue], ensure_ascii=False), backend.prompts[2])
        self.assertIn("historical review findings, not current facts", backend.prompts[2])
        self.assertEqual(prior_review_findings({"first": [issue], "second": [issue, {"reason": "structural only"}]}), [issue])

    def test_graph_id_diagnostic_names_path_and_preserves_strict_validation(self):
        value = insights(include=True)
        value["architecture"]["nodes"][0]["id"] = "entry_point"
        value["architecture"]["edges"][0]["from"] = "entry_point"
        issues = validate_insights(value, article(), packet())
        self.assertEqual(len(issues), 1)
        self.assertIn("/insights/architecture/nodes/0/id", issues[0])
        self.assertIn("no underscores", issues[0])
        self.assertIn("edge from/to", issues[0])


if __name__ == "__main__":
    unittest.main()
