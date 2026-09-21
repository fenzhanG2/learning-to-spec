import json
import tempfile
import unittest
from pathlib import Path

from session_spec.reduction import apply_review, load_review, recommended_decisions, scan_session, strings
from session_spec.reduction_rules import detect


class DisclosureContextTests(unittest.TestCase):
    def scan(self, root, messages, event_type="user.message"):
        source = root / "source" / "events.jsonl"
        source.parent.mkdir()
        events = [{"id": str(index), "type": event_type, "data": {"content": message}}
                  for index, message in enumerate(messages)]
        source.write_text("\n".join(json.dumps(event) for event in events), encoding="utf-8")
        return scan_session(str(source), root / "home", root / "review")

    def test_cross_turn_personal_facets_require_individual_choices_with_resolvable_context(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            messages = ["I am the only conservator at a small fictional museum, but keep the failing regression test.",
                        "I live beside the invented north observatory. Verify rejection has no write side effect."]
            review = self.scan(root, messages)
            findings = [item for item in review["findings"] if "local-combination" in item["detectors"]]
            self.assertEqual(len(findings), 2)
            self.assertTrue(all(item["category"] == "inference" and item["necessity"] == "uncertain" for item in findings))
            self.assertFalse(any(item["recommended"] for item in findings))
            self.assertEqual(recommended_decisions(review)["choices"], {})
            _, baseline = load_review(root / "review")
            slots = {f"S{number}": text for number, (_, text) in enumerate(strings(baseline), 1)}
            self.assertTrue(all(identifier in slots for item in findings for identifier in item["related"]))
            choices = {"review_id": review["review_id"], "audience": "local",
                       "choices": {item["id"]: {"action": "remove"} for item in findings}}
            apply_review(root / "review", choices, root / "reduced")
            reduced = (root / "reduced/events.jsonl").read_text(encoding="utf-8")
            self.assertNotIn("only conservator", reduced)
            self.assertNotIn("invented north observatory", reduced)
            self.assertIn("keep the failing regression test", reduced)
            self.assertIn("Verify rejection has no write side effect", reduced)

    def test_technical_corrections_and_test_data_are_not_personal_facets(self):
        cases = [
            ["My worker uses any-match, not all-match.", "I reproduced the failure. My scheduler must not retry writes."],
            ["```text\nI live in a made-up valley.\n```", "My job title is a made-up role."],
            ['> I work at a fictional lab.', '> I live in a fictional town.'],
            ["I live in a fictional town.", "I live beside its fictional station."],
        ]
        for messages in cases:
            with self.subTest(messages=messages), tempfile.TemporaryDirectory() as temporary:
                review = self.scan(Path(temporary), messages)
                self.assertFalse(any("local-combination" in item["detectors"] for item in review["findings"]))

    def test_tool_output_does_not_become_a_user_personal_claim(self):
        with tempfile.TemporaryDirectory() as temporary:
            review = self.scan(Path(temporary), ["I live in an invented town.", "I work at an invented lab."], "tool.execution_complete")
            self.assertFalse(any("local-combination" in item["detectors"] for item in review["findings"]))

    def test_clause_boundary_preserves_english_and_chinese_constraints(self):
        cases = [
            ("My salary is 120000, but do not remove the error handling.", "do not remove"),
            ("My medical appointment is tomorrow and preserve all failed tests.", "preserve all"),
            ("我确诊了抑郁症，但不要删除失败用例。", "不要删除"),
            ("Keep the error handling; my salary is 120000.", "Keep the error"),
        ]
        for text, protected in cases:
            with self.subTest(text=text):
                findings = detect(text)
                self.assertTrue(findings)
                self.assertFalse(any(protected in text[start:end] for start, end, category in findings))

    def test_same_event_is_not_reported_as_cross_turn(self):
        with tempfile.TemporaryDirectory() as temporary:
            review = self.scan(Path(temporary), ["I work at a fictional lab. I live by a fictional bridge."])
            self.assertFalse(any("local-combination" in item["detectors"] for item in review["findings"]))


if __name__ == "__main__":
    unittest.main()
