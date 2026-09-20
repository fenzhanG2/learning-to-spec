import base64
import json
import secrets
import tempfile
import unittest
from pathlib import Path

from session_spec.reduction import apply_review, load_review, recommended_decisions, scan_session, secure_baseline, transform
from session_spec.reduction_rules import detect
from session_spec.reduction_semantic import semantic_review


class Backend:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []
        self.prompts = []

    def generate(self, prompt, label):
        self.calls.append({"label": label})
        self.prompts.append(prompt)
        return self.responses.pop(0)


class ReductionTests(unittest.TestCase):
    def test_legacy_recommendations_cannot_bypass_current_individual_choice_policy(self):
        findings = [
            {"id": "personal", "category": "health", "text": "Private appointment", "necessity": "unnecessary", "recommended": "remove", "detectors": ["copilot"]},
            {"id": "broad", "category": "environment", "text": "path\nDo not deploy", "recommended": "remove"},
            {"id": "uncertain", "category": "identifier", "text": "person", "necessity": "uncertain", "recommended": "pseudonymize", "detectors": ["copilot"]},
            {"id": "identifier", "category": "identifier", "text": "fake@example.invalid", "recommended": "pseudonymize", "detectors": ["local"]},
        ]
        review = {"review_id": "legacy", "audience": "local", "findings": findings}
        self.assertEqual(recommended_decisions(review)["choices"], {"identifier": {"action": "pseudonymize"}})
        self.assertEqual(findings[0]["recommended"], "remove")

    def fixture(self, root, messages):
        source = root / "source/events.jsonl"
        source.parent.mkdir()
        events = [{"id": str(index), "type": "user.message", "data": {"content": text}} for index, text in enumerate(messages)]
        events.append({"id": "tool-1", "type": "tool.execution_complete", "data": {"toolCallId": "call-1", "toolName": "Bash", "success": False, "result": {"content": "Build failed: exit code 1. Keep the failure in the handoff."}}})
        source.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")
        review = scan_session(str(source), root / "copilot", root / "review", audience="root")
        return source, review

    def test_hard_secrets_hidden_reasoning_and_encoded_secrets_are_removed_locally(self):
        invented = "".join(("ghp_", secrets.token_hex(20)))
        encoded = base64.b64encode(invented.encode()).decode()
        value = {"password": secrets.token_hex(12), "reasoningText": "DO_NOT_EXPORT_THINKING", "data": {"content": invented + " " + encoded, "public": "npm test -- --runInBand"}}
        reduced = secure_baseline(value)
        self.assertNotIn(invented, json.dumps(reduced))
        self.assertNotIn(encoded, json.dumps(reduced))
        self.assertNotIn("DO_NOT_EXPORT_THINKING", json.dumps(reduced))
        self.assertEqual(reduced["data"]["public"], value["data"]["public"])
        self.assertEqual(secure_baseline(reduced), reduced)

    def test_binary_document_data_uris_are_excluded_not_only_images(self):
        payload = base64.b64encode(b"SYNTHETIC_PRIVATE_DOCUMENT").decode()
        for media in ("application/pdf", "application/octet-stream", "text/plain;charset=utf-8", "image/png"):
            value = "Attachment: data:" + media + ";base64," + payload + "\nKeep the failed test."
            reduced = secure_baseline(value)
            self.assertNotIn(payload, reduced)
            self.assertIn("Keep the failed test.", reduced)

    def test_namespaced_credentials_database_urls_and_cookie_headers(self):
        invented = ["SYNTHETIC_" + secrets.token_hex(12) for index in range(7)]
        value = {"OPENAI_API_KEY": invented[0], "GITHUB_CLIENT_SECRET": invented[1],
                 "AWS_SECRET_ACCESS_KEY": invented[2], "STRIPE_SECRET_KEY": invented[3],
                 "content": f"postgresql://test:{invented[4]}@db.example.invalid/project\nredis://:{invented[5]}@localhost:6379\nSet-Cookie: session={invented[6]}; HttpOnly\nKeep the failed test.",
                 "password_required": True, "success": False}
        reduced = secure_baseline(value)
        self.assertNotIn("SYNTHETIC_", json.dumps(reduced))
        self.assertIn("postgresql://", reduced["content"])
        self.assertIn("Keep the failed test.", reduced["content"])
        self.assertTrue(reduced["password_required"])
        self.assertFalse(reduced["success"])
        self.assertEqual(secure_baseline(reduced), reduced)

    def test_choices_are_required_and_original_failures_are_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, review = self.fixture(root, ["Contact finch@example.invalid. My salary is 123000. I am an idiot! Keep the failing test and its exit code."])
            original = source.read_bytes()
            self.assertTrue({"identifier", "financial", "reputation"} <= {item["category"] for item in review["findings"]})
            with self.assertRaises(ValueError):
                apply_review(root / "review", {"review_id": review["review_id"], "audience": "root", "choices": {}}, root / "refused")
            decisions = recommended_decisions(review)
            for finding in review["findings"]:
                if finding["id"] not in decisions["choices"]:
                    decisions["choices"][finding["id"]] = {"action": "remove"}
            receipt = apply_review(root / "review", decisions, root / "reduced")
            output = (root / "reduced/events.jsonl").read_text(encoding="utf-8")
            for private in ("finch@example.invalid", "My salary is 123000", "I am an idiot"):
                self.assertNotIn(private, output)
            self.assertIn("Keep the failing test and its exit code", output)
            self.assertIn('"success": false', output)
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(receipt["audience"], "root")

    def test_source_change_and_wrong_audience_invalidate_choices(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, review = self.fixture(root, ["Email finch@example.invalid"])
            decisions = recommended_decisions(review)
            decisions["audience"] = "team:another"
            with self.assertRaises(ValueError):
                apply_review(root / "review", decisions, root / "reduced")
            source.write_text(source.read_text() + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "changed"):
                load_review(root / "review")

    def test_repeated_entities_nested_tool_sources_and_generalization(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, review = self.fixture(root, ["Reach finch@example.invalid", "Again finch@example.invalid"])
            finding = next(item for item in review["findings"] if item["text"] == "finch@example.invalid")
            self.assertEqual(len(finding["occurrences"]), 2)
            decisions = recommended_decisions(review)
            decisions["choices"][finding["id"]] = {"action": "generalize", "replacement": "the project contact"}
            _, baseline = load_review(root / "review")
            reduced, operations = transform(review, baseline, decisions)
            self.assertEqual(json.dumps(reduced).count("the project contact"), 2)
            self.assertIn("[GENERALIZED_DETAIL_", json.dumps(reduced))
            decisions["choices"][finding["id"]]["replacement"] = "api_key=ThisIsSyntheticButSecret123456"
            with self.assertRaises(ValueError):
                transform(review, baseline, decisions)

    def test_domain_code_and_technical_failures_are_not_personal_disclosures(self):
        for text in ("The health endpoint failed with HTTP 503.", "Add a salary column to the synthetic schema.", "The OAuth password field must remain required.", "The user corrected the approach after the build failed.", "Implement depression screening validation using fixture data."):
            self.assertEqual(detect(text), [], text)

    def test_semantic_review_requires_consent_and_exact_unique_quotes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, review = self.fixture(root, ["My private appointment is at the specialist clinic. Contact finch@example.invalid."])
            backend = Backend([])
            with self.assertRaisesRegex(ValueError, "consent"):
                semantic_review(root / "review", backend=backend)
            self.assertEqual(backend.calls, [])
            bad = {"findings": [{"slot": "S1", "quote": "Invented private sentence", "category": "health", "necessity": "uncertain", "reason": "Risk", "alternative": "", "related": []}]}
            backend = Backend([bad, bad])
            before = (root / "review/review.json").read_bytes()
            with self.assertRaises(ValueError):
                semantic_review(root / "review", consent=True, backend=backend)
            self.assertEqual((root / "review/review.json").read_bytes(), before)
            self.assertTrue(all("finch@example.invalid" not in prompt for prompt in backend.prompts))

    def test_semantic_findings_propagate_to_repeated_source_and_change_approval_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            quote = "A private appointment at the specialist clinic."
            source, review = self.fixture(root, [quote, quote])
            response = {"findings": [{"slot": "S1", "quote": quote, "category": "inference", "necessity": "uncertain", "reason": "This scheduling aside could reveal private context; verify its relevance.", "alternative": "A scheduling constraint.", "related": ["S1"]}], "limitations": []}
            newer = semantic_review(root / "review", consent=True, backend=Backend([response]))
            self.assertNotEqual(newer["review_id"], review["review_id"])
            self.assertEqual(len(newer["findings"][0]["occurrences"]), 2)
            choices = recommended_decisions(newer)
            self.assertEqual(choices["choices"], {})
            with self.assertRaisesRegex(ValueError, "explicit choice"):
                apply_review(root / "review", choices, root / "reduced")
            choices["choices"][newer["findings"][0]["id"]] = {"action": "remove"}
            apply_review(root / "review", choices, root / "reduced")
            self.assertNotIn(quote, (root / "reduced/events.jsonl").read_text())

    def test_repeated_literal_within_one_slot_is_grouped_not_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            quote = "privatecolleague staff"
            self.fixture(root, [quote + " file1\n" + quote + " file2"])
            response = {"findings": [{"slot": "S1", "quote": quote, "category": "identifier", "necessity": "unnecessary", "reason": "An account in tool output.", "alternative": "", "related": []}]}
            review = semantic_review(root / "review", consent=True, backend=Backend([response]))
            self.assertEqual(len(review["findings"][0]["occurrences"]), 2)

    def test_keep_cannot_be_silently_overridden_by_larger_removal(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root, ["My confidential customer is finch@example.invalid."])
            review, baseline = load_review(root / "review")
            decisions = recommended_decisions(review)
            identifier = next(item for item in review["findings"] if item["category"] == "identifier")
            for finding in review["findings"]:
                decisions["choices"][finding["id"]] = {"action": "remove"}
            decisions["choices"][identifier["id"]] = {"action": "keep"}
            with self.assertRaisesRegex(ValueError, "overlaps"):
                transform(review, baseline, decisions)

    def test_dotted_addresses_do_not_hide_the_surrounding_private_sentence(self):
        text = "My confidential customer is finch@example.invalid. Keep the failed test."
        spans = [(text[start:end], category) for start, end, category in detect(text)]
        self.assertTrue(any(category == "confidential" for literal, category in spans))
        self.assertFalse(any("Keep the failed test" in literal for literal, category in spans))

    def test_local_personal_context_cannot_be_bulk_decided(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, review = self.fixture(root, ["My salary is 120001. I am an idiot! Keep the failed test."])
            self.assertTrue(review["findings"])
            decisions = recommended_decisions(review)
            self.assertEqual(decisions["choices"], {})
            with self.assertRaisesRegex(ValueError, "explicit choice"):
                apply_review(root / "review", decisions, root / "reduced")

    def test_model_confidence_cannot_authorize_bulk_personal_removal(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            quote = "I feel like a fraud. Keep the failed test."
            self.fixture(root, [quote])
            response = {"findings": [{"slot": "S1", "quote": quote, "category": "reputation", "necessity": "unnecessary", "reason": "Potential private aside, overbroad proposal.", "alternative": "Keep the failed test.", "related": []}]}
            review = semantic_review(root / "review", consent=True, backend=Backend([response]))
            self.assertEqual(recommended_decisions(review)["choices"], {})

    def test_chinese_private_aside_can_be_removed_without_losing_technical_instruction(self):
        text = "我确诊了抑郁症。请保留失败用例，修复 HTTP 500。"
        findings = detect(text)
        self.assertTrue(any(category == "health" for start, end, category in findings))
        for start, end, category in findings:
            self.assertNotIn("HTTP 500", text[start:end])

    def test_hidden_fields_never_become_keepable_findings(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, review = self.fixture(root, ["api_key=ThisIsSyntheticSensitive123456"])
            self.assertGreater(review["hard_removals"]["secret_pattern_matches"], 0)
            self.assertFalse(any("ThisIsSyntheticSensitive" in finding["text"] for finding in review["findings"]))


if __name__ == "__main__":
    unittest.main()
