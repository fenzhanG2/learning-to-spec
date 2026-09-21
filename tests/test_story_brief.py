import copy
import unittest

from offline_provider import guard_offline_test
from session_spec.storage import PROMPTS
from session_spec.story_brief import CONSTRAINT_KINDS, validate_brief
from test_story_pipeline import brief, packet


class BriefContractTests(unittest.TestCase):
    def setUp(self):
        guard_offline_test(self)
        self.events = packet()
        self.events[0]["human_input"] += "不要卸载软件。"
        self.candidate = brief()
        self.candidate["non_goals"] = [{"text": "不卸载软件。", "quote": "不要卸载软件", "refs": ["E000001"]}]
        self.candidate["constraints"].append({"kind": "environment", "text": "记录有入口读回。", "refs": ["E000002"]})

    def test_all_nonempty_optional_shapes_remain_supported(self):
        self.assertEqual(validate_brief(self.candidate, self.events), [])
        for field in ("non_goals", "scope", "constraints"):
            self.assertTrue(self.candidate[field])

    def test_guessed_kind_and_extra_fields_have_exact_path_schema_and_enum(self):
        self.candidate["constraints"][0].update(kind="constraint", quote="extra")
        errors = validate_brief(self.candidate, self.events)
        self.assertTrue(any('/brief/constraints/0: expected an object with exactly keys ["kind", "refs", "text"]' in issue for issue in errors))
        self.assertTrue(any('/brief/constraints/0/kind: expected one of ["requirement", "environment"]' in issue for issue in errors))
        self.assertTrue(any("source provenance" in issue for issue in errors))

    def test_missing_kind_and_wrong_types_fail_without_guessing(self):
        for kind in (None, False, [], {}, "constraint", "proposed"):
            candidate = copy.deepcopy(self.candidate)
            if kind is None:
                candidate["constraints"][0].pop("kind")
            else:
                candidate["constraints"][0]["kind"] = kind
            with self.subTest(kind=kind):
                errors = validate_brief(candidate, self.events)
                self.assertTrue(any("/brief/constraints/0/kind" in issue for issue in errors))

    def test_human_provenance_and_exact_exclusion_quote_remain_required(self):
        for field in ("goals", "non_goals", "constraints"):
            candidate = copy.deepcopy(self.candidate)
            candidate[field][0]["refs"] = ["E000002"]
            with self.subTest(field=field):
                errors = validate_brief(candidate, self.events)
                self.assertTrue(any(f"/brief/{field}/0/refs" in issue and "human_input" in issue for issue in errors))
        self.candidate["non_goals"][0]["quote"] = "Unrecorded exclusion"
        self.assertTrue(any("/brief/non_goals/0/quote" in issue and "exact substring" in issue for issue in validate_brief(self.candidate, self.events)))

    def test_statement_array_and_root_diagnostics_are_actionable(self):
        variants = [("problem", None, "/brief/problem", '"refs", "text"'),
                    ("scope", [None], "/brief/scope/0", '"refs", "text"'),
                    ("non_goals", [{}], "/brief/non_goals/0", '"quote", "refs", "text"'),
                    ("constraints", [{}], "/brief/constraints/0", '"kind", "refs", "text"'),
                    ("goals", [], "/brief/goals", "1–3 items")]
        for field, value, pointer, expected in variants:
            candidate = copy.deepcopy(self.candidate)
            candidate[field] = value
            with self.subTest(field=field):
                self.assertTrue(any(pointer in issue and expected in issue for issue in validate_brief(candidate, self.events)))
        self.candidate["unknown"] = True
        self.assertTrue(any('/brief: expected schema story-brief/v1 and exactly keys' in issue for issue in validate_brief(self.candidate, self.events)))

    def test_reference_and_visible_text_guards_are_not_relaxed(self):
        for key, value, pointer in (("refs", [], "refs"), ("refs", ["E999999"], "refs"),
                                    ("text", "", "text"), ("text", "x" * 301, "text"),
                                    ("text", "E000001", "text"), ("text", "<b>markup</b>", "text")):
            candidate = copy.deepcopy(self.candidate)
            candidate["scope"][0][key] = value
            with self.subTest(key=key, value=value):
                self.assertTrue(any(f"/brief/scope/0/{pointer}" in issue for issue in validate_brief(candidate, self.events)))

    def test_fast_prompt_covers_nonempty_optional_shapes_and_provenance(self):
        prompt = (PROMPTS / "fast-story.md").read_text(encoding="utf-8")
        for kind in CONSTRAINT_KINDS:
            self.assertIn(kind, prompt)
        for text in ('Each non_goals item is {"text":', '"quote":"exact human words"',
                     'Each constraints item is {"kind":"requirement"', "kind is ONLY requirement or environment",
                     "must cite human_input", "do not empty arrays", 'Optional chapter details use {"title":',
                     "Continuation and recipe items both require basis explicit/proposed", "procedure entries are nonempty strings"):
            self.assertIn(text, prompt)


if __name__ == "__main__":
    unittest.main()
