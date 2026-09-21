import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from offline_provider import guard_offline_test
from session_spec.native_bridge import PIPELINE, NativeBridge, native_preparation_phase


class NativeProgressTests(unittest.TestCase):
    def setUp(self):
        guard_offline_test(self)
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.work = self.root / "abstraction/story/_support/.work"
        self.work.mkdir(parents=True)

    def tearDown(self):
        self.temporary.cleanup()

    def checkpoint(self, name, value="PRIVATE_MODEL_TEXT"):
        path = self.work / name
        path.write_text(value, encoding="utf-8")
        return path

    def attempt(self, repairs, **extra):
        return self.checkpoint("edition-attempt.json", json.dumps({
            "status": "running", "repair_counts": {"patches": repairs},
            "failures": {"PRIVATE": [{"reason": "PRIVATE_MODEL_TEXT"}]}, **extra,
        }))

    def native_status(self, stage="scan", status="running", mode="llm", pipeline=PIPELINE):
        job = {"id": "a" * 32, "directory": self.root, "pipeline": pipeline, "privacy_mode": mode}
        snapshot = {"job": job["id"], "stage": stage, "status": status, "next_action": "unchanged"}
        bridge = NativeBridge.__new__(NativeBridge)
        bridge.studio = SimpleNamespace(job=lambda identifier: job, snapshot=lambda identifier: dict(snapshot))
        return bridge.call("status", {"job": job["id"]})

    def test_draft_until_nonempty_joint_checkpoint_exists(self):
        self.assertEqual(self.native_status()["phase"], "draft")
        self.checkpoint("joint-draft.json", "")
        self.assertEqual(self.native_status()["phase"], "draft")
        self.checkpoint("joint-draft.json")
        self.assertEqual(self.native_status()["phase"], "checking")

    def test_draft_body_is_never_read_or_published(self):
        self.checkpoint("joint-draft.json")
        with patch.object(Path, "open", side_effect=AssertionError("No model body reads")):
            result = self.native_status()
        self.assertEqual(result["phase"], "checking")
        self.assertNotIn("PRIVATE", json.dumps(result))
        self.assertEqual(set(result), {"job", "stage", "status", "next_action", "phase"})

    def test_started_repair_has_no_completed_patch_yet(self):
        self.checkpoint("joint-draft.json")
        self.attempt(1)
        self.assertEqual(self.native_status()["phase"], "repair")

    def test_completed_patch_returns_to_checking_for_probe_or_review(self):
        self.checkpoint("joint-draft.json")
        self.attempt(1)
        self.checkpoint("edition-patch-3.json")
        self.assertEqual(self.native_status()["phase"], "checking")

    def test_later_outstanding_repair_and_temporary_files(self):
        self.checkpoint("joint-draft.json")
        self.attempt(2)
        self.checkpoint("edition-patch-1.json")
        self.checkpoint("edition-patch-2.json", "")
        self.checkpoint("edition-patch-2.json.tmp")
        self.checkpoint("edition-patch-private.json")
        self.assertEqual(self.native_status()["phase"], "repair")
        self.checkpoint("edition-patch-4.json")
        self.assertEqual(self.native_status()["phase"], "checking")

    def test_no_repairs_is_checking(self):
        self.checkpoint("joint-draft.json")
        self.attempt(0)
        self.assertEqual(self.native_status()["phase"], "checking")

    def test_edition_receipt_is_checking_not_privacy_or_done(self):
        self.checkpoint("joint-draft.json")
        self.checkpoint("edition-receipt.json")
        self.assertEqual(self.native_status()["phase"], "checking")
        self.assertEqual(self.native_status()["status"], "running")

    def test_manifest_gates_privacy_without_reading_private_surfaces(self):
        (self.root / "abstraction/abstraction.json").write_text("PRIVATE_METADATA", encoding="utf-8")
        with patch.object(Path, "open", side_effect=AssertionError("No private surface reads")):
            result = self.native_status()
        self.assertEqual(result["phase"], "privacy")
        self.assertNotIn("PRIVATE", json.dumps(result))

    def test_full_or_unknown_privacy_mode_never_claims_a_privacy_scan(self):
        (self.root / "abstraction/abstraction.json").write_text("PRIVATE_METADATA", encoding="utf-8")
        for mode in ("full", None, "PRIVATE_UNKNOWN"):
            with self.subTest(mode=mode):
                self.assertNotIn("phase", self.native_status(mode=mode))

    def test_legacy_review_alone_does_not_claim_new_pipeline_privacy(self):
        (self.root / "review").mkdir()
        (self.root / "review/review.json").write_text("PRIVATE_OLD_REVIEW", encoding="utf-8")
        self.assertEqual(self.native_status()["phase"], "draft")
        self.assertNotIn("phase", self.native_status(pipeline=None))

    def test_incomplete_oversized_or_invalid_receipt_omits_phase(self):
        self.checkpoint("joint-draft.json")
        for contents in ('{"status":', "x" * (2 * 1024 * 1024 + 1), "[]", "null"):
            with self.subTest(length=len(contents)):
                self.checkpoint("edition-attempt.json", contents)
                self.assertNotIn("phase", self.native_status())

    def test_untrusted_receipt_strings_and_invalid_counts_are_not_projected(self):
        self.checkpoint("joint-draft.json")
        for repairs in (True, -1, 101, "PRIVATE_MODEL_TEXT", None):
            with self.subTest(repairs=repairs):
                self.attempt(repairs, phase="PRIVATE_PHASE")
                self.assertNotIn("phase", self.native_status())
        self.attempt(1, status="PRIVATE_STATUS")
        self.assertNotIn("phase", self.native_status())

    def test_io_failures_do_not_break_status(self):
        with patch.object(Path, "lstat", side_effect=PermissionError("PRIVATE_PATH")):
            result = self.native_status()
        self.assertEqual(result["status"], "running")
        self.assertNotIn("phase", result)
        self.assertNotIn("PRIVATE", json.dumps(result))
        self.checkpoint("joint-draft.json")
        self.attempt(1)
        with patch.object(Path, "open", side_effect=OSError("PRIVATE_READ_ERROR")):
            self.assertNotIn("phase", self.native_status())

    def test_other_stages_and_terminal_statuses_do_not_read_checkpoints(self):
        for stage, status in (("generate", "running"), ("scan", "done"), ("scan", "error"), ("scan", "new")):
            with self.subTest(stage=stage, status=status), patch.object(Path, "lstat", side_effect=AssertionError("No phase IO")):
                result = self.native_status(stage=stage, status=status)
                self.assertNotIn("phase", result)
                self.assertEqual(result["status"], status)

    def test_checkpoint_directory_instead_of_file_is_unknown(self):
        (self.work / "joint-draft.json").mkdir()
        self.assertNotIn("phase", self.native_status())

    def test_symbolic_checkpoint_is_not_followed(self):
        target = self.root / "private-target"
        target.write_text("PRIVATE_TARGET", encoding="utf-8")
        try:
            (self.work / "joint-draft.json").symlink_to(target)
        except OSError:
            self.skipTest("Symlink creation unavailable")
        with patch.object(Path, "open", side_effect=AssertionError("Linked target must not be read")):
            self.assertNotIn("phase", self.native_status())

    def test_repeated_projection_is_read_only(self):
        self.checkpoint("joint-draft.json")
        path = self.attempt(1)
        before = path.read_bytes(), path.stat().st_mtime_ns
        self.assertEqual(self.native_status()["phase"], "repair")
        self.assertEqual(self.native_status()["phase"], "repair")
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)

    def test_missing_workspace_is_unknown(self):
        self.assertIsNone(native_preparation_phase(self.root / "missing", "llm"))

    def test_fast_draft_check_and_repair_use_only_checkpoint_metadata(self):
        support = self.work.parent
        attempt = support / "fast-attempt.json"
        attempt.write_text(json.dumps({"attempts": [{"index": 0}], "private": "PRIVATE_TEXT"}))
        self.assertEqual(self.native_status()["phase"], "draft")
        (support / "fast-candidate-0.json").write_text("PRIVATE_MODEL_OUTPUT")
        self.assertEqual(self.native_status()["phase"], "checking")
        attempt.write_text(json.dumps({"attempts": [{"index": 0}, {"index": 1}]}))
        self.assertEqual(self.native_status()["phase"], "repair")
        (support / "fast-candidate-1.json").write_text("PRIVATE_MODEL_OUTPUT")
        self.assertEqual(self.native_status()["phase"], "checking")
        self.assertNotIn("PRIVATE", json.dumps(self.native_status()))

    def test_fast_full_content_quality_is_checking_not_privacy(self):
        (self.work.parent / "fast-attempt.json").write_text("PRIVATE_NOT_READ")
        (self.root / "abstraction/abstraction.json").write_text("PRIVATE_NOT_READ")
        with patch.object(Path, "open", side_effect=AssertionError("No private surface reads")):
            self.assertEqual(self.native_status(mode="full")["phase"], "checking")
            self.assertEqual(self.native_status(mode="llm")["phase"], "privacy")

    def test_isolated_source_review_and_draft_privacy_are_distinct_progress_phases(self):
        support = self.work.parent
        (self.root / "abstraction/abstraction.json").write_text("PRIVATE_NOT_READ")
        (support / "source-review-started.json").write_text("PRIVATE_NOT_READ")
        with patch.object(Path, "open", side_effect=AssertionError("No private surface reads")):
            self.assertEqual(self.native_status(mode="llm")["phase"], "checking")
        (support / "fast-quality.json").write_bytes(b"done")
        with patch.object(Path, "open", side_effect=AssertionError("No private surface reads")):
            self.assertEqual(self.native_status(mode="llm")["phase"], "privacy")

    def test_fast_partial_or_unexpected_receipt_is_unknown(self):
        attempt = self.work.parent / "fast-attempt.json"
        for payload in ('{', '[]', '{"attempts":[{"index":true}]}', '{"attempts":[{"index":9}]}'):
            attempt.write_text(payload)
            self.assertNotIn("phase", self.native_status())


if __name__ == "__main__":
    unittest.main()
