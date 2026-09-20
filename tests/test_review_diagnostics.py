import json
import tempfile
import unittest
from pathlib import Path

from session_spec.backend import ModelResponseError
from session_spec.model_io import generate_json
from session_spec.story_grounding import field_has_quote, quote_diagnostic, resolve_review_locations, validate_grounding


class ReviewDiagnosticsTests(unittest.TestCase):
    def test_empty_field_can_ground_an_omission_without_fabricating_content(self):
        edition = {"brief": {"constraints": []}}
        review = {"issues": [{"path": "/brief/constraints", "quote": "[]", "kind": "contract", "contract_quote": "Preserve real constraints", "evidence": []}]}
        self.assertEqual(validate_grounding(review, edition, [], "Preserve real constraints"), [])
        self.assertEqual(resolve_review_locations(review, edition, []), (review, []))
        self.assertFalse(field_has_quote([], " "))
        self.assertFalse(field_has_quote(["constraint"], "[]"))
        self.assertFalse(field_has_quote({}, "[]"))
        self.assertTrue(field_has_quote({}, "{}"))

    def test_diagnostics_guide_but_do_not_relax_literal_grounding(self):
        field = "This is a production build report."
        self.assertFalse(field_has_quote(field, "A production build report."))
        hint = quote_diagnostic(field, "A production build report.")
        self.assertIn(field, hint)
        self.assertIn("not an automatic replacement", hint)

    def test_both_invalid_model_responses_survive_the_bounded_format_retry(self):
        class Backend:
            def __init__(self):
                self.calls = []

            def generate(self, prompt, label):
                self.calls.append(label)
                raise ModelResponseError("Unterminated string", '{"value":"incomplete')

        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            backend = Backend()
            with self.assertRaises(ModelResponseError):
                generate_json(backend, "Original prompt", "review", directory)
            self.assertEqual(len(backend.calls), 2)
            for name in ("review-invalid-response-1.json", "review-format-retry-invalid-response-2.json"):
                self.assertEqual(json.loads((directory / name).read_bytes())["response"], '{"value":"incomplete')
