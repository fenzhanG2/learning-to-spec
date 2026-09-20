import copy
import json
import tempfile
import unittest
from pathlib import Path

from session_spec.minimization_review import MAX_FOCUS_CHARS, MAX_GROUPS, SCHEMA, minimization_focus, validate_minimization
from session_spec.reduction import digest, transform
from session_spec.story_article import validate_article
from session_spec.story_editor import CHECKS, generate_edition, validate_review
from test_story_pipeline import FakeBackend, article, brief, edition_review, insights, packet


def mixed_source():
    private = "I am an idiot!"
    original = "保留功能，只隐藏窗口。 " + private + " 测试失败两次；不要部署。"
    review = {"review_id": "synthetic", "audience": "local", "findings": [
        {"id": "aside", "text": private, "occurrences": [
            {"path": [0, "content"], "start": original.index(private), "end": original.index(private) + len(private), "field_sha256": digest(original)}]}]}
    reduced, operations = transform(review, [{"content": original}], {
        "review_id": "synthetic", "audience": "local", "choices": {"aside": {"action": "remove"}}})
    events = packet()
    events[0].update(text=reduced[0]["content"], human_input=reduced[0]["content"])
    return events, private, operations


def decision(status="excluded", authored=None):
    return {"ref": "E000001", "status": status, "source_quote": "[PRIVATE_DETAIL_1]",
            "authored": authored or [], "note": "Retain technical failure and the no-deployment constraint, not unrelated personal context."}


class MinimizationReviewTests(unittest.TestCase):
    def test_focus_uses_reduced_mixed_sentence_not_original_private_values(self):
        events, private, operations = mixed_source()
        draft = {"article": article(), "brief": brief(), "insights": insights()}
        original = copy.deepcopy((draft, events))
        focus = minimization_focus(draft, events)
        self.assertEqual(operations, {"remove": 1})
        self.assertEqual(focus["schema"], SCHEMA)
        self.assertEqual(focus["marked_event_count"], 1)
        self.assertNotIn(private, json.dumps(focus, ensure_ascii=False))
        self.assertIn("测试失败两次；不要部署", json.dumps(focus, ensure_ascii=False))
        self.assertTrue(any(claim["path"].startswith("/brief/") for claim in focus["groups"][0]["claims"]))
        self.assertTrue(any("/agent_detail/" in claim["path"] for claim in focus["groups"][0]["claims"]))
        self.assertEqual((draft, events), original)

    def test_marker_window_survives_long_source_middle_and_metadata_is_excluded(self):
        events = [{"ref": "E000001", "type": "user.message", "human_input": "a" * 7000 + " [ENTITY_1] " + "z" * 7000,
                   "text": "UNUSED_PRIVATE_TEXT", "reasoningText": "HIDDEN_VALUE", "origin": {"path": "LOCAL_PATH"}}]
        focus = minimization_focus({}, events)
        self.assertIn("[ENTITY_1]", json.dumps(focus))
        for value in ("UNUSED_PRIVATE_TEXT", "HIDDEN_VALUE", "LOCAL_PATH"):
            self.assertNotIn(value, json.dumps(focus))
        for segment in focus["groups"][0]["source_windows"]:
            self.assertEqual(segment["text"], events[0]["human_input"][segment["start"]:segment["end"]])

    def test_focus_bounds_and_omissions_do_not_claim_exhaustive_coverage(self):
        events = [{"ref": f"E{index:06d}", "type": "assistant.message", "text": "x" * 200 + "[REDACTED]" + "y" * 900} for index in range(1, 120)]
        draft = {"article": {"chapters": [{"markdown": "Authored context " * 300, "refs": [event["ref"]]} for event in events]}}
        focus = minimization_focus(draft, events)
        self.assertLessEqual(len(json.dumps(focus, ensure_ascii=False)), MAX_FOCUS_CHARS)
        self.assertLessEqual(len(focus["groups"]), MAX_GROUPS)
        self.assertEqual(len(focus["groups"]) + focus["omitted_groups"], len(events))
        self.assertGreater(focus["omitted_groups"], 0)

    def test_ordinary_words_are_not_markers_or_automatic_rejections(self):
        events = [{"ref": "E000001", "type": "user.message", "human_input": "Fix private-method redaction and aside placeholder styles."}]
        focus = minimization_focus({}, events)
        self.assertEqual(focus["groups"], [])
        self.assertEqual(validate_minimization({}, {}, events, focus), [])

    def test_privacy_engineering_negative_control_preserves_necessary_literal_example(self):
        text = "The redaction regression must emit [PRIVATE_DETAIL_1] while preserving the failed test."
        events = [{"ref": "E000001", "type": "user.message", "human_input": text}]
        draft = {"article": {"agent_markdown": "Check `[PRIVATE_DETAIL_1]` and the failed-test output. E000001"}}
        focus = minimization_focus(draft, events)
        review = {"issues": [], "minimization": [decision("necessary", [{"path": "/article/agent_markdown", "quote": "Check `[PRIVATE_DETAIL_1]`"}])]}
        review["minimization"][0]["note"] = "The marker syntax is itself the requested regression assertion, not a private disclosure."
        self.assertEqual(validate_minimization(review, draft, events, focus), [])

    def test_every_indexed_event_needs_a_literal_accounting_and_needs_fix_cannot_pass(self):
        events, private, operations = mixed_source()
        draft = {"article": {"agent_markdown": "A private aside was removed."}}
        focus = minimization_focus(draft, events)
        self.assertTrue(validate_minimization({}, draft, events, focus))
        review = {"issues": [], "minimization": [decision("needs_fix", [{"path": "/article/agent_markdown", "quote": "A private aside was removed."}])]}
        self.assertTrue(validate_minimization(review, draft, events, focus))
        review["issues"] = [{"category": "data_minimization"}]
        self.assertEqual(validate_minimization(review, draft, events, focus), [])
        for field, value in (("source_quote", private), ("status", []), ("authored", [{"path": "/missing", "quote": "invented"}])):
            invalid = copy.deepcopy(review)
            invalid["minimization"][0][field] = value
            self.assertTrue(validate_minimization(invalid, draft, events, focus))

    def test_new_dimension_preserves_six_and_seven_check_legacy_receipts(self):
        for protocol, count in ((None, 6), ("grounded-findings/v2", 6), ("grounded-findings/v3", 7), ("grounded-findings/v4", 8)):
            review = edition_review()
            review["checked"] = review["checked"][:count]
            self.assertEqual(validate_review(review, protocol=protocol), [])
        review = edition_review()
        review["checked"] = review["checked"][:-1]
        self.assertTrue(validate_review(review))
        self.assertEqual(CHECKS[-1], "data_minimization")
        self.assertTrue(validate_review(review, protocol=[]))

    def test_grounded_pipeline_repair_preserves_failure_and_binds_focus_to_candidate(self):
        events, private, operations = mixed_source()
        draft = {"article": article(), "brief": brief(), "insights": insights()}
        path = "/article/chapters/0/markdown"
        residue = "A private aside was omitted."
        draft["article"]["chapters"][0]["markdown"] += " " + residue + " 测试失败两次；不要部署。"
        review = edition_review([{"path": path, "quote": residue, "kind": "contract", "category": "data_minimization",
                                  "contract_quote": "隐私裁剪不是故事情节。", "evidence": [], "reason": "Do not narrate the existence of an irrelevant removed aside."}])
        review["minimization"] = [decision("needs_fix", [{"path": path, "quote": residue}])]
        clean = edition_review()
        clean["minimization"] = [decision()]
        correction = draft["article"]["chapters"][0]["markdown"].replace(" " + residue, "")
        backend = FakeBackend([review, {"patches": [{"op": "replace", "path": path, "value": correction}]}, clean])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            actual = generate_edition(root, draft, events, backend, validate_article, language="auto")
            self.assertNotIn(residue, actual["article"]["chapters"][0]["markdown"])
            self.assertIn("测试失败两次；不要部署", actual["article"]["chapters"][0]["markdown"])
            self.assertEqual(actual["article"]["agent_detail"], draft["article"]["agent_detail"])
            receipt = json.loads((root / "edition-receipt.json").read_bytes())
            focus = json.loads((root / "edition-minimization-1.json").read_bytes())
            self.assertEqual(receipt["identity"]["minimization_focus"], SCHEMA)
            self.assertEqual(receipt["candidate_sha256"], focus["candidate_sha256"])
            self.assertNotIn(private, "\n".join(backend.prompts))
            self.assertIn("MINIMIZATION_FOCUS", backend.prompts[0])
            cached = FakeBackend([])
            generate_edition(root, draft, events, cached, validate_article, language="auto")
            self.assertFalse(cached.calls)
            receipt["review"]["minimization"] = []
            (root / "edition-receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
            rechecked = FakeBackend([clean])
            generate_edition(root, draft, events, rechecked, validate_article, language="auto")
            self.assertEqual(len(rechecked.calls), 1)

    def test_missing_contract_quote_uses_existing_bounded_retry_before_any_patch(self):
        events, private, operations = mixed_source()
        draft = {"article": article(), "brief": brief(), "insights": insights()}
        path = "/article/chapters/0/markdown"
        residue = "A private aside was omitted."
        draft["article"]["chapters"][0]["markdown"] += " " + residue
        issue = {"path": path, "quote": residue, "kind": "contract", "category": "data_minimization", "evidence": [], "reason": "Unnecessary private metadata."}
        invalid = edition_review([issue])
        invalid["minimization"] = [decision("needs_fix", [{"path": path, "quote": residue}])]
        corrected = copy.deepcopy(invalid)
        corrected["issues"][0]["contract_quote"] = "隐私裁剪不是故事情节。"
        clean = edition_review()
        clean["minimization"] = [decision()]
        patch = {"patches": [{"op": "replace", "path": path, "value": "任务调用包装器。"}]}
        with tempfile.TemporaryDirectory() as temporary:
            backend = FakeBackend([invalid, corrected, patch, clean])
            actual = generate_edition(Path(temporary), draft, events, backend, validate_article)
            self.assertNotIn(residue, actual["article"]["chapters"][0]["markdown"])
            self.assertEqual([call["label"] for call in backend.calls], ["story-edition-review", "story-edition-review-grounding-retry", "story-edition-patch", "story-edition-review"])
            self.assertIn("Supplied category rule for navigation", backend.prompts[1])
            self.assertIn("contract_quote is missing or not literal", backend.prompts[1])
            self.assertNotIn(private, "\n".join(backend.prompts))


if __name__ == "__main__":
    unittest.main()
