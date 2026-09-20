import copy
import json
import unittest

from session_spec.rationale_audit import rationale_focus, validate_rationale_audit
from session_spec.story_grounding import source_quote_diagnostic, source_quote_origins, validate_grounding
from test_rationale_audit import audit, fixture


class QuoteDiagnosticTests(unittest.TestCase):
    def test_tool_argument_authorship_does_not_change_observable_origin(self):
        draft, events = fixture()
        events[0] = {"ref": "E000001", "type": "tool.execution_start", "tool": "AskUserQuestion",
                     "arguments": {"question": "Keep the wrapper because it preserves startup?"},
                     "reasoningText": "HIDDEN_REASON_CANARY"}
        review = audit()
        span = review["rationale_audit"][0]["evidence"][0]
        span.update(origin="assistant", quote="because it preserves startup?")
        before = copy.deepcopy((review, events))
        focus = rationale_focus(draft, events)
        self.assertEqual(focus["sources"][0]["quote_origins"], ["tool"])
        errors = validate_rationale_audit(review, focus, events)
        self.assertIn("literal matches at this ref: ['tool']", errors[0])
        self.assertNotIn("HIDDEN_REASON_CANARY", json.dumps((focus, errors)))
        self.assertEqual((review, events), before)
        span["origin"] = "tool"
        self.assertEqual(validate_rationale_audit(review, focus, events), [])

    def test_markdown_mismatch_shows_literal_line_but_remains_rejected(self):
        event = {"ref": "E000003", "type": "assistant.message", "text": "Earlier.\n**Choose this:** because it preserves startup.\nLater."}
        quote = "Choose this: because it preserves startup."
        message = source_quote_diagnostic(event, "assistant", quote)
        self.assertIn("**Choose this:** because it preserves startup.", message)
        self.assertIn("literal matches at this ref: []", message)
        edition = {"article": {"title": "Keep the wrapper"}}
        review = {"issues": [{"path": "/article/title", "quote": "wrapper", "kind": "source_fact",
                              "evidence": [{"ref": "E000003", "origin": "assistant", "quote": quote}]}]}
        self.assertTrue(validate_grounding(review, edition, [event], ""))
        review["issues"][0]["evidence"][0]["quote"] = "**Choose this:** because it preserves startup."
        self.assertEqual(validate_grounding(review, edition, [event], ""), [])

    def test_tool_result_is_not_human_authority_without_actual_human_input(self):
        event = {"ref": "E000007", "type": "tool.execution_complete", "result": {"content": "user selected yes"}}
        self.assertEqual(source_quote_origins(event), ["tool"])
        event["human_input"] = "yes"
        self.assertEqual(source_quote_origins(event), ["human", "tool"])
        self.assertNotIn("literal matches at this ref: ['human']", source_quote_diagnostic(event, "human", "user selected yes"))

    def test_hint_is_bounded_and_preserves_escapes(self):
        event = {"ref": "E000009", "type": "tool.execution_start", "arguments": {"text": "Keep \\n literal " + "x" * 10000}}
        message = source_quote_diagnostic(event, "tool", "Keep\n literal")
        self.assertLess(len(message), 1400)
        self.assertIn("\\\\n", message)
        self.assertIn("literal matches at this ref: []", message)

    def test_diagnostic_does_not_fix_unknown_reference_oversize_or_wrong_origin(self):
        draft, events = fixture()
        for reference, origin, quote in (("E999999", "human", "Keep"), ("E000001", "human", "x" * 501),
                                         ("E000001", "assistant", "because it preserves the existing startup path")):
            review = audit()
            review["rationale_audit"][0]["evidence"] = [{"ref": reference, "origin": origin, "quote": quote}]
            before = copy.deepcopy(review)
            self.assertTrue(validate_rationale_audit(review, rationale_focus(draft, events), events))
            self.assertEqual(review, before)

    def test_hostile_origins_are_not_echoed_or_truncated_into_a_valid_role(self):
        draft, events = fixture()
        for origin in ("human" + "X" * 100000, {"human": "X" * 100000}, ["tool"] * 10000):
            review = audit()
            review["rationale_audit"][0]["evidence"][0]["origin"] = origin
            errors = validate_rationale_audit(review, rationale_focus(draft, events), events)
            self.assertIn("<invalid origin>", errors[0])
            self.assertLessEqual(len(errors[0]), 1900)
            self.assertNotIn("XXX", errors[0])
            self.assertEqual(review["rationale_audit"][0]["evidence"][0]["origin"], origin)

    def test_final_diagnostic_budget_includes_escaped_metadata_and_source(self):
        event = {"ref": "\x00" * 10000, "type": "\x00" * 10000, "text": "\x00" * 10000,
                 "human_input": "\x00" * 10000}
        message = source_quote_diagnostic(event, "human", "Missing literal")
        self.assertLessEqual(len(message), 1800)
        self.assertIn("does not change the quote", message)


if __name__ == "__main__":
    unittest.main()
