import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

from native_abstract_fixture import mocked_abstraction
from offline_provider import guard_offline_test
from session_spec.native_bridge import PIPELINE, NativeBridge
from session_spec.reduction import prepare_full_session


class NativeReviewSummaryTests(unittest.TestCase):
    def setUp(self):
        guard_offline_test(self)
        self.temporary = tempfile.TemporaryDirectory()
        self.session_id = str(uuid.uuid4())
        self.bridge = NativeBridge(self.session_id, self.temporary.name)
        self.secret = "ghp_" + "S" * 30
        self.events = [{"type": "session.start", "data": {"sessionId": self.session_id}},
                       {"type": "user.message", "data": {"content": "Synthetic fix: " + self.secret, "reasoningText": "HIDDEN"}}]

    def tearDown(self):
        self.bridge.close()
        self.temporary.cleanup()

    def prepare(self, mode="full"):
        captured = self.bridge.call("capture", {"events": self.events, "privacy_mode": mode, "capture_order": PIPELINE})
        with mocked_abstraction() as drafting:
            result = self.bridge.call("scan", {"session": captured["source"], "readers": "both", "delivery": "local", "audience": "local",
                                               "privacy_mode": mode, "detection": "none" if mode == "full" else "copilot",
                                               "semantic": mode == "llm", "pipeline": PIPELINE})
            self.bridge.studio.drain()
        self.assertEqual(drafting.call_count, 1)
        return self.bridge.studio.job(result["job"]), captured

    def test_both_captures_defer_rules_and_keep_original_observable_text(self):
        for mode in ("full", "llm"):
            captured = self.bridge.call("capture", {"events": self.events, "privacy_mode": mode, "capture_order": PIPELINE})
            source = Path(self.bridge.studio.sessions[-1]["path"])
            self.assertIn(self.secret, source.read_text(encoding="utf-8"))
            self.assertNotIn("HIDDEN", source.read_text(encoding="utf-8"))
            receipt = json.loads((source.parent / "capture.json").read_bytes())
            self.assertEqual(receipt["schema"], "native-session-capture/v2")
            self.assertEqual(receipt["capture_order"], PIPELINE)
            self.assertEqual(receipt["source_sha256"], captured["sha256"])
            self.assertEqual(receipt["hard_removals"], {"secret_fields": 0, "secret_pattern_matches": 0})

    def test_bound_review_distinguishes_original_snapshot_from_reviewed_artifact(self):
        job, captured = self.prepare()
        visible = self.bridge.call("review", {"job": job["id"]})
        self.assertEqual(visible["capture_rule_matches"], {"secret_fields": 0, "secret_pattern_matches": 0})
        self.assertEqual(visible["pipeline"], PIPELINE)
        self.assertEqual(visible["original_source_sha256"], captured["sha256"])
        self.assertNotEqual(visible["source_sha256"], captured["sha256"])
        pending = self.bridge.call("pending_review", {})["review"]
        self.assertEqual(pending["snapshot_sha256"], captured["sha256"])
        self.assertEqual(pending["review_source_sha256"], visible["source_sha256"])
        self.assertEqual(pending["abstraction_sha256"], visible["abstraction_sha256"])

    def test_old_capture_or_missing_order_is_rejected_before_preparation(self):
        for data in ({"events": self.events, "privacy_mode": "full"},
                     {"events": self.events, "privacy_mode": "full", "capture_order": "scan-first"}):
            with self.assertRaisesRegex(ValueError, "abstract-first"):
                self.bridge.call("capture", data)
        captured = self.bridge.capture(self.events, "full")
        with patch("session_spec.native_bridge.prepare_abstract_review", side_effect=AssertionError("No preparation")):
            with self.assertRaisesRegex(ValueError, "abstract-first"):
                self.bridge.call("scan", {"session": captured["source"], "privacy_mode": "full"})

    def test_legacy_reviews_remain_saved_but_never_resume_silently(self):
        self.bridge.capture(self.events, "full")
        source = Path(self.bridge.studio.sessions[0]["path"])
        identifier = uuid.uuid4().hex
        directory = self.bridge.root / "jobs" / identifier
        prepare_full_session(source, self.bridge.studio.home, directory / "review", "local",
                             {"readers": "both", "destination": "local"})
        job = {"id": identifier, "directory": directory, "stage": "scan", "status": "done"}
        self.bridge.studio.jobs[identifier] = job
        original = (directory / "review/review.json").read_bytes()
        self.assertEqual(self.bridge.call("pending_review", {}), {"review": None, "legacy_reviews_unavailable": 1})
        with self.assertRaisesRegex(ValueError, "predates abstract-first"):
            self.bridge.call("review", {"job": identifier})
        self.assertEqual((directory / "review/review.json").read_bytes(), original)
        self.assertNotIn("generation", job)

    def test_manifest_artifact_surface_capture_and_pipeline_tampering_fail_closed(self):
        job, captured = self.prepare()
        source = Path(self.bridge.studio.sessions[0]["path"])
        targets = [source, source.parent / "capture.json", job["directory"] / "review/review.json",
                   job["directory"] / "abstraction/abstraction.json", job["directory"] / "abstraction/source/events.jsonl",
                   job["directory"] / "abstraction/story/agent-spec.md"]
        for target in targets:
            original = target.read_bytes()
            try:
                if target.name == "capture.json":
                    binding = json.loads(original)
                    binding["schema"] = "native-session-capture/v1"
                    target.write_bytes(json.dumps(binding).encode())
                elif target.name == "review.json":
                    binding = json.loads(original)
                    binding.pop("pipeline")
                    target.write_bytes(json.dumps(binding).encode())
                else:
                    target.write_bytes(original + b"\n")
                with self.subTest(target=target.name), self.assertRaises(ValueError):
                    self.bridge.call("pending_review", {})
            finally:
                target.write_bytes(original)

    def test_render_requires_exact_draft_binding_before_worker_dispatch(self):
        job, captured = self.prepare()
        review = self.bridge.call("review", {"job": job["id"]})
        data = {"job": job["id"], "pipeline": PIPELINE, "review_id": review["review_id"], "choices": {}, "confirmed": True}
        with patch.object(self.bridge.studio, "action", return_value={"job": job["id"]}) as dispatch:
            with self.assertRaisesRegex(ValueError, "abstraction changed"):
                self.bridge.call("generate", data)
            dispatch.assert_not_called()
            self.bridge.call("generate", {**data, "abstraction_sha256": review["abstraction_sha256"]})
            self.assertEqual(dispatch.call_count, 1)


if __name__ == "__main__":
    unittest.main()
