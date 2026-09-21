import copy
import unittest

from offline_provider import guard_offline_test
from session_spec.fast_story import structural_issues
from session_spec.storage import PROMPTS
from test_fast_story import draft
from test_story_pipeline import packet


class FastContractTests(unittest.TestCase):
    def setUp(self):
        guard_offline_test(self)
        self.candidate = draft()
        self.events = packet()
        self.prompt = (PROMPTS / "fast-story.md").read_text(encoding="utf-8")
        self.assertEqual(structural_issues(self.candidate, self.events, "auto"), [])

    def test_route_and_heading_contract_matches_whole_edition_gates(self):
        self.candidate["article"]["route"] *= 6
        self.assertTrue(any("at most five" in issue for issue in structural_issues(self.candidate, self.events, "auto")))
        self.candidate = draft()
        self.candidate["article"]["agent_markdown"] = "# Handoff\n\n## History\nReported E000001 E000002."
        self.assertTrue(any("must start with handoff state" in issue for issue in structural_issues(self.candidate, self.events, "auto")))
        self.assertIn("article.route has 1–5 turning points", self.prompt)
        self.assertIn("first '## ' section must not be history", self.prompt)

    def test_usage_cardinality_nested_refs_and_tool_name_match_are_explicit(self):
        original = self.candidate["article"]["agent_detail"]["trajectory"][0]["tool_steps"][0]["usage"]
        for usage, diagnostic in (([], "1–4 usage"), (original * 5, "1–4 usage"),
                                   ([{**original[0], "refs": ["E000001"]}], "must belong to this step"),
                                   ([{**original[0], "tool": "invented_tool"}], "does not match tools")):
            with self.subTest(usage=usage):
                self.candidate["article"]["agent_detail"]["trajectory"][0]["tool_steps"][0]["usage"] = usage
                self.assertTrue(any(diagnostic in issue for issue in structural_issues(self.candidate, self.events, "auto")))
        for text in ("usage has 1–4 entries", "belongs to that step's tool_refs", "belong to its phase's tool_refs", "tool name must match"):
            self.assertIn(text, self.prompt)

    def test_tool_step_repetition_and_phase_ref_mismatch_remain_rejected(self):
        phase = self.candidate["article"]["agent_detail"]["trajectory"][0]
        phase["tool_steps"].append(copy.deepcopy(phase["tool_steps"][0]))
        self.assertTrue(any("repeat a tool call" in issue for issue in structural_issues(self.candidate, self.events, "auto")))
        phase["tool_steps"].pop()
        phase["tool_refs"] = []
        self.assertTrue(any("must include its inline step tool_refs" in issue for issue in structural_issues(self.candidate, self.events, "auto")))
        self.assertIn("Do not repeat a tool call/event across steps", self.prompt)

    def test_unique_ids_and_crosslinks_are_required_not_display_titles(self):
        self.candidate["article"]["chapters"].append(copy.deepcopy(self.candidate["article"]["chapters"][0]))
        self.assertTrue(any("Duplicate or reserved chapter ID" in issue for issue in structural_issues(self.candidate, self.events, "auto")))
        self.candidate = draft()
        self.candidate["article"]["agent_detail"]["paths"][0]["phase_ids"] = ["E000001"]
        self.assertTrue(any("real trajectory phase IDs" in issue for issue in structural_issues(self.candidate, self.events, "auto")))
        self.assertIn("chapter IDs and phase IDs must each be unique", self.prompt)
        self.assertIn("never titles or source refs", self.prompt)

    def test_omitted_graph_has_no_placeholder_fields(self):
        self.candidate["insights"]["architecture"] = {"decision": "omit", "reason": "No useful graph.", "nodes": []}
        self.assertTrue(any("must not contain a placeholder graph" in issue for issue in structural_issues(self.candidate, self.events, "auto")))
        self.candidate["insights"]["architecture"].pop("nodes")
        self.assertEqual(structural_issues(self.candidate, self.events, "auto"), [])
        self.assertIn("omitted architecture has ONLY decision and reason", self.prompt)

    def test_insights_prose_limits_and_markup_boundary_are_explicit(self):
        for value, diagnostic in (("x" * 2001, "Text too long"), ("<b>claim</b>", "markup")):
            with self.subTest(value=value):
                self.candidate["insights"]["closing"]["paragraphs"] = [value]
                self.assertTrue(any(diagnostic in issue for issue in structural_issues(self.candidate, self.events, "auto")))
        self.assertIn("insights prose fields/paragraphs <=2,000 characters", self.prompt)
        self.assertIn("Brief and insights prose contains no HTML markup", self.prompt)

    def test_auto_language_metadata_is_not_a_translation_instruction(self):
        self.assertIn("requested=auto", self.prompt)
        self.assertIn("presentation metadata, not a translation instruction", self.prompt)
        self.assertIn("Preserve source quotes, commands, code, identifiers and schema enums literally", self.prompt)

    def test_scope_example_uses_cited_objects_not_uncited_strings(self):
        self.assertIn('"scope":[{"text":', self.prompt)
        self.candidate["brief"]["scope"] = ["workflow matrix"]
        issues = structural_issues(self.candidate, self.events, "auto")
        self.assertTrue(any('/brief/scope/0: expected an object with exactly keys ["refs", "text"]' in issue for issue in issues))


if __name__ == "__main__":
    unittest.main()
