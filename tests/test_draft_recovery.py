import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from offline_provider import guard_offline_test
from session_spec.draft_recovery import create_recovery, recovery_snapshot
from session_spec.fast_quality import QualityReviewFailure
from session_spec.fast_story import DraftValidationError
from session_spec.native_bridge import NativeBridge, PIPELINE
from session_spec.storage import file_hash, write_json


class DraftRecoveryTests(unittest.TestCase):
    def setUp(self):
        guard_offline_test(self)
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.support = self.root / "abstraction/story/_support"
        self.support.mkdir(parents=True)
        self.candidate = {"article": {"opening": "The CI matrix is incomplete. <script>SECRET</script>",
                                     "chapters": [{"title": "Next step", "markdown": "Inspect PATH", "refs": ["BAD_REF"]}],
                                     "agent_markdown": "# Resume\n![remote](https://example.invalid/SECRET)\n```\nnot executed",
                                     "agent_detail": {"resume": {"next_action": "Inspect PATH", "refs": []}}},
                          "brief": {"problem": {"text": "The runner was not checked", "refs": ["BAD_REF"]}}}
        write_json(self.support / "fast-candidate-0.json", self.candidate)

    def tearDown(self):
        self.temporary.cleanup()

    def test_local_pair_keeps_content_and_errors_without_approval_active_html_or_source_dump(self):
        before = file_hash(self.support / "fast-candidate-0.json")
        identifier = create_recovery(self.root, "both", "draft_references_invalid")
        directory, snapshot = recovery_snapshot(self.root, identifier)
        self.assertEqual(set(snapshot["files"]), {"human-spec.html", "agent-spec.md", "deliverables.zip"})
        human = (directory / "deliverables/human-spec.html").read_text(encoding="utf-8")
        agent = (directory / "deliverables/agent-spec.md").read_text(encoding="utf-8")
        self.assertIn("UNVALIDATED PRIVATE DRAFT", human)
        self.assertIn("Privacy review/redaction is NOT complete", agent)
        self.assertIn("one automatic repair", human)
        self.assertIn("Inspect PATH", human)
        self.assertIn("&lt;script&gt;SECRET&lt;/script&gt;", human)
        self.assertNotIn("<script>", human)
        self.assertNotIn("BAD_REF", human)
        self.assertIn("````text\n# Resume", agent)
        self.assertEqual(file_hash(self.support / "fast-candidate-0.json"), before)
        with zipfile.ZipFile(directory / "deliverables.zip") as archive:
            self.assertEqual(set(archive.namelist()), {"human-spec.html", "agent-spec.md"})
        self.assertFalse((self.root / "generation").exists())
        self.assertFalse((self.root / "package").exists())

    def test_recovery_respects_selected_reader_and_refuses_overwrite_or_file_tamper(self):
        identifier = create_recovery(self.root, "agent", "draft_structure_invalid")
        directory, snapshot = recovery_snapshot(self.root, identifier)
        self.assertNotIn("human-spec.html", snapshot["files"])
        with self.assertRaises(OSError):
            create_recovery(self.root, "agent", "draft_structure_invalid")
        (directory / "deliverables/agent-spec.md").write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "changed"):
            recovery_snapshot(self.root, identifier)

    def test_malformed_json_is_preserved_as_an_explicit_incomplete_draft_not_executed(self):
        other = self.root / "malformed"
        support = other / "abstraction/story/_support"
        support.mkdir(parents=True)
        write_json(support / "fast-invalid-response-0.json", {"invalid_response": '{"opening":"partial <img src=https://example.invalid>...', "issues": ["Invalid JSON"]})
        identifier = create_recovery(other, "human", "draft_structure_invalid")
        directory, snapshot = recovery_snapshot(other, identifier)
        human = (directory / "deliverables/human-spec.html").read_text(encoding="utf-8")
        self.assertIn("partial &lt;img", human)
        self.assertNotIn("<img", human)
        self.assertEqual(set(snapshot["files"]), {"human-spec.html", "deliverables.zip"})

    def test_bound_receipt_rejects_changed_manifest_and_missing_drafts(self):
        identifier = create_recovery(self.root, "human", "draft_structure_invalid")
        path = self.root / "unvalidated-draft/manifest.json"
        record = json.loads(path.read_bytes())
        record["upload_allowed"] = True
        write_json(path, record)
        with self.assertRaises(ValueError):
            recovery_snapshot(self.root, identifier)
        with self.assertRaises(ValueError):
            recovery_snapshot(self.root, file_hash(path))
        with self.assertRaisesRegex(ValueError, "No generated draft"):
            create_recovery(self.root / "empty", "human", "draft_structure_invalid")

    def test_native_failure_delivers_only_a_local_unapproved_draft_and_no_publication_authority(self):
        for cause in ("structure", "quality"):
            with self.subTest(cause=cause):
                bridge = NativeBridge("11111111-1111-4111-8111-111111111111", self.root / cause)
                source = bridge.capture([{"type": "session.start", "data": {"sessionId": bridge.session_id}},
                                         {"type": "user.message", "data": {"content": "Check the synthetic PATH"}}], "llm")

                def failed_review(source, home, review_directory, audience, selection, mode, **settings):
                    support = review_directory.parent / "abstraction/story/_support"
                    support.mkdir(parents=True)
                    write_json(support / "fast-candidate-0.json", self.candidate)
                    if cause == "quality":
                        write_json(support / "fast-quality.json", {"result": {"issues": [{"reason": "PRIVATE_DIAGNOSTIC: wrong speaker"}]}})
                        raise QualityReviewFailure("PRIVATE_DIAGNOSTIC")
                    raise DraftValidationError("PRIVATE_DIAGNOSTIC", ["Agent continuation needs real source refs"])

                try:
                    with patch("session_spec.native_bridge.prepare_abstract_review", side_effect=failed_review):
                        scanned = bridge.call("scan", {"session": source["source"], "pipeline": PIPELINE, "readers": "both",
                                                       "delivery": "artifactstore", "audience": "root", "privacy_mode": "llm",
                                                       "detection": "copilot", "semantic": True})
                        bridge.studio.drain()
                    status = bridge.call("status", scanned)
                    self.assertEqual(status["status"], "error")
                    self.assertTrue(status["draft_available"])
                    self.assertNotIn("PRIVATE", json.dumps(status))
                    self.assertEqual(bridge.studio.public_progress(scanned["job"])["phase"], "draft_available")
                    output = bridge.studio.output_delivery(scanned["job"])
                    self.assertEqual(output["kind"], "unvalidated_draft")
                    self.assertNotIn("local", output)
                    delivered = bridge.call("deliverables", scanned)
                    self.assertTrue(Path(delivered["folder"]).is_dir())
                    with patch("session_spec.native_bridge.open_local_output") as opener:
                        bridge.studio.open_output(scanned["job"], output["snapshot_id"], "human-spec.html")
                    opener.assert_called_once()
                    self.assertNotIn("generation", bridge.studio.job(scanned["job"]))
                    for operation in ("review", "package", "plan", "publish"):
                        with self.assertRaises((ValueError, KeyError)):
                            bridge.call(operation, scanned)
                finally:
                    bridge.close()


if __name__ == "__main__":
    unittest.main()
