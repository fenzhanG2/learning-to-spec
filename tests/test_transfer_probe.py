import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from session_spec.storage import write_json
from session_spec.story_editor import generate_edition, validate_feedback
from session_spec.story_pipeline import validate_article
from session_spec.transfer_probe import (effective_feedback, fingerprint, run_probe, validate_probe,
                                         validate_record, validate_resolutions)
from test_story_pipeline import FakeBackend, article, brief, edition_review, insights, packet, transfer_review


def draft():
    return {"article": article(), "brief": brief(), "insights": insights()}


def finding():
    return {"reason": "The scope may require reading the entry before acting.", "risk": "A receiver might assume current state.",
            "anchors": [{"file": "agent-spec.md", "quote": "入口已改变"}], "refs": ["E000002"]}


def resolution(index=0, status="not_applicable"):
    return {"index": index, "status": status, "note": "The handoff already bounds the observation; the recorded readback supports that limit.",
            "kind": "source_fact", "path": "/article/agent_markdown", "quote": "入口已改变",
            "evidence": [{"ref": "E000002", "origin": "tool", "quote": "startup signal observed"}]}


def review_with_resolution(index=0, status="not_applicable", issues=None):
    review = edition_review(issues)
    review["feedback_resolution"] = [resolution(index, status)]
    return review


class TransferProbeTests(unittest.TestCase):
    def test_reader_gets_only_the_delivered_pair_and_no_full_source_or_human_draft(self):
        events = packet() + [{"ref": "E000099", "type": "assistant.message", "text": "UNSELECTED_SOURCE_CANARY"}]
        candidate = draft()
        candidate["article"]["title"] = "HUMAN_ONLY_DRAFT_CANARY"
        with tempfile.TemporaryDirectory() as temporary:
            backend = FakeBackend([transfer_review([finding()])])
            record = run_probe(candidate, events, backend, Path(temporary))
            self.assertEqual(validate_record(record, events), [])
            self.assertEqual(record["identity"]["candidate_sha256"], fingerprint(candidate))
            self.assertNotIn("UNSELECTED_SOURCE_CANARY", backend.prompts[0])
            self.assertNotIn("HUMAN_ONLY_DRAFT_CANARY", backend.prompts[0])
            self.assertNotIn("HISTORICAL_EVENTS", backend.prompts[0])
            self.assertIn("DELIVERED_PAIR_DATA", backend.prompts[0])
            self.assertEqual(record["calls"], backend.calls)

    def test_probe_rejects_invented_file_quotes_refs_and_reserved_properties(self):
        documents = {"agent-spec.md": "入口已改变 E000002", "evidence.md": "recorded result E000002"}
        self.assertEqual(validate_probe(transfer_review([finding()]), documents), [])
        mutations = [lambda item: item["anchors"][0].update(file="_support/private.json"),
                     lambda item: item["anchors"][0].update(quote="A fabricated quote"),
                     lambda item: item.update(refs=["E000099"]),
                     lambda item: item.update(origin="external"),
                     lambda item: item.update(anchors=[])]
        for mutate in mutations:
            candidate = finding()
            mutate(candidate)
            with self.subTest(candidate=candidate):
                self.assertTrue(validate_probe(transfer_review([candidate]), documents))
        incomplete = transfer_review()
        incomplete["checked"].pop()
        self.assertTrue(validate_probe(incomplete, documents))
        self.assertTrue(validate_probe(transfer_review([finding()] * 13), documents))

    def test_one_probe_retry_preserves_bad_findings_and_does_not_silently_accept(self):
        bad = transfer_review([finding()])
        bad["findings"][0]["anchors"][0]["quote"] = "ABSENT"
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            backend = FakeBackend([bad, transfer_review()])
            record = run_probe(draft(), packet(), backend, directory)
            self.assertEqual(len(record["attempts"]), 2)
            self.assertTrue(record["attempts"][0]["errors"])
            self.assertEqual([item["label"] for item in backend.calls], ["story-transfer-probe", "story-transfer-probe-citation-retry"])
            backend = FakeBackend([bad, bad])
            with self.assertRaisesRegex(ValueError, "bounded citation"):
                run_probe(draft(), packet(), backend, directory)
            failed = json.loads((directory / "edition-transfer-probe.json").read_bytes())
            self.assertEqual(failed["status"], "failed")
            self.assertEqual(len(failed["calls"]), 2)

    def test_size_and_backend_budgets_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            backend = FakeBackend([])
            with patch("session_spec.transfer_probe.MAX_DOCUMENT_CHARACTERS", 1):
                with self.assertRaisesRegex(ValueError, "no silent truncation"):
                    run_probe(draft(), packet(), backend, Path(temporary))
            self.assertEqual(backend.calls, [])
            backend = FakeBackend([ValueError("Model-call budget exhausted")])
            with self.assertRaisesRegex(ValueError, "budget exhausted"):
                generate_edition(Path(temporary), draft(), packet(), backend, validate_article, transfer_probe=True)
            self.assertFalse((Path(temporary) / "edition.json").exists())
            self.assertEqual(len(backend.calls), 1)

    def test_false_positive_is_source_adjudicated_not_automatically_patched(self):
        backend = FakeBackend([transfer_review([finding()]), review_with_resolution()])
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            actual = generate_edition(directory, draft(), packet(), backend, validate_article, transfer_probe=True)
            self.assertEqual(actual, draft())
            self.assertEqual(len(backend.calls), 2)
            receipt = json.loads((directory / "edition-receipt.json").read_bytes())
            self.assertEqual(receipt["review"]["feedback_resolution"][0]["status"], "not_applicable")
            self.assertEqual(len(effective_feedback(receipt, [], packet())), 1)
            self.assertIn("First candidate entering source review", receipt["transfer_probe"]["scope"])
            cached = FakeBackend([])
            self.assertEqual(generate_edition(directory, draft(), packet(), cached, validate_article, transfer_probe=True), draft())
            self.assertEqual(cached.calls, [])

    def test_effective_feedback_combines_origins_and_cache_validates_all_resolutions(self):
        feedback = [{"reason": "External bounded observation", "refs": ["E000001"]}]
        accepted = review_with_resolution(index=1)
        accepted["feedback_resolution"].insert(0, {"index": 0, "status": "not_applicable", "note": "Already limited"})
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            backend = FakeBackend([transfer_review([finding()]), accepted])
            generate_edition(directory, draft(), packet(), backend, validate_article, feedback=feedback, transfer_probe=True)
            receipt = json.loads((directory / "edition-receipt.json").read_bytes())
            self.assertEqual(effective_feedback(receipt, feedback, packet())[0], feedback[0])
            self.assertEqual(receipt["effective_feedback"][1]["origin"], "transfer-probe/v1")
            cached = FakeBackend([])
            generate_edition(directory, draft(), packet(), cached, validate_article, feedback=feedback, transfer_probe=True)
            self.assertEqual(cached.calls, [])
            corrupt = copy.deepcopy(receipt)
            corrupt["effective_feedback"].pop()
            with self.assertRaisesRegex(ValueError, "effective reader feedback"):
                effective_feedback(corrupt, feedback, packet())
            receipt["review"]["feedback_resolution"].pop()
            write_json(directory / "edition-receipt.json", receipt)
            restarted = FakeBackend([transfer_review([finding()]), accepted])
            generate_edition(directory, draft(), packet(), restarted, validate_article, feedback=feedback, transfer_probe=True)
            self.assertEqual(len(restarted.calls), 2)

    def test_unsubstantiated_dismissal_cannot_pass_the_review(self):
        invalid = review_with_resolution()
        invalid["feedback_resolution"][0]["evidence"][0]["quote"] = "fabricated source"
        backend = FakeBackend([transfer_review([finding()]), invalid, invalid])
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            with self.assertRaisesRegex(ValueError, "Transfer adjudication"):
                generate_edition(directory, draft(), packet(), backend, validate_article, transfer_probe=True)
            self.assertFalse((directory / "edition.json").exists())
            self.assertEqual(len(backend.calls), 3)
        self.assertTrue(validate_resolutions(invalid, [{"origin": "transfer-probe/v1"}], draft(), packet(), ""))
        for malformed in (None, "invalid", [None], [{"index": True}], []):
            with self.subTest(resolutions=malformed):
                self.assertTrue(validate_resolutions({"feedback_resolution": malformed}, [{"origin": "transfer-probe/v1"}], draft(), packet(), ""))

    def test_repaired_candidate_is_source_checked_without_claiming_a_second_probe(self):
        issue = {"reason": "Clarify the bounded state"}
        failed = review_with_resolution(status="needs_fix", issues=[issue])
        passed = review_with_resolution(status="fixed")
        backend = FakeBackend([transfer_review([finding()]), failed,
                               {"patches": [{"op": "replace", "path": "/article/title", "value": "保留入口边界"}]}, passed])
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            actual = generate_edition(directory, draft(), packet(), backend, validate_article, transfer_probe=True)
            self.assertEqual(actual["article"]["title"], "保留入口边界")
            self.assertEqual([item["label"] for item in backend.calls].count("story-transfer-probe"), 1)
            receipt = json.loads((directory / "edition-receipt.json").read_bytes())
            self.assertEqual(receipt["transfer_probe"]["identity"]["candidate_sha256"], fingerprint(draft()))
            self.assertNotEqual(fingerprint(actual), fingerprint(draft()))
            self.assertEqual(receipt["calls"], backend.calls)

    def test_source_snapshot_tampering_and_external_impersonation_fail(self):
        with tempfile.TemporaryDirectory() as temporary:
            record = run_probe(draft(), packet(), FakeBackend([transfer_review()]), Path(temporary))
            tampered = copy.deepcopy(record)
            tampered["documents"]["agent-spec.md"] += "changed"
            self.assertTrue(validate_record(tampered, packet()))
            self.assertTrue(validate_record(record, packet()[:-1]))
            tampered = copy.deepcopy(record)
            tampered["result"]["summary"] += "changed"
            self.assertTrue(validate_record(tampered, packet()))
            with self.assertRaisesRegex(ValueError, "impersonate"):
                validate_feedback({"source_sha256": "source", "issues": [{"origin": "transfer-probe/v1", "reason": "fake"}]}, packet(), "source")


if __name__ == "__main__":
    unittest.main()
