import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from native_abstract_fixture import mocked_abstraction
from offline_provider import guard_offline_test
from test_private_sharing import Client
from session_spec.artifacts import PRIVATE, destination_plan, publish_package
from session_spec.draft_recovery import create_recovery, prepare_recovery_package, recovery_snapshot
from session_spec.native_bridge import NativeBridge, PIPELINE
from session_spec.reduction import digest
from session_spec.reduction_semantic import PrivacyReviewFailure
from session_spec.share_package import approve_package, load_package, package_bytes
from session_spec.storage import write_json


class RecoveryPublicationTests(unittest.TestCase):
    def setUp(self):
        guard_offline_test(self)
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.support = self.root / "abstraction/story/_support"
        self.support.mkdir(parents=True)
        self.candidate = {"article": {"opening": "Synthetic private draft only", "agent_markdown": "Resume the synthetic test. Fictional personal aside."}}
        write_json(self.support / "fast-candidate-0.json", self.candidate)

    def tearDown(self):
        self.temporary.cleanup()

    def test_selected_draft_bytes_require_two_explicit_overrides_and_are_never_quality_approved(self):
        for readers in ("human", "agent", "both"):
            with self.subTest(readers=readers):
                job = self.root / readers
                support = job / "abstraction/story/_support"
                support.mkdir(parents=True)
                write_json(support / "fast-candidate-0.json", self.candidate)
                identifier = create_recovery(job, readers, "draft_privacy_invalid")
                target = job / "package"
                for accepted in (False, "true", 1):
                    with self.assertRaisesRegex(ValueError, "Explicit acceptance"):
                        prepare_recovery_package(job, identifier, target, "root", accepted)
                manifest = prepare_recovery_package(job, identifier, target, "root", True)
                self.assertEqual(manifest["quality"], "unvalidated")
                files = {name: (target / "files" / name).read_bytes() for name in manifest["files"]}
                client = Client(files)
                plan = destination_plan(target, "synthetic-draft", client, preview=True)
                self.assertEqual(client.writes, [])
                with self.assertRaisesRegex(ValueError, "override"):
                    approve_package(target, manifest["package_id"], [], confirmed_publish=True)
                with self.assertRaises(OSError):
                    publish_package(target, plan, plan["plan_id"], client)
                self.assertEqual(client.writes, [])
                approve_package(target, manifest["package_id"], [], confirmed_publish=True, accept_unvalidated=True)
                receipt = publish_package(target, plan, plan["plan_id"], client)
                self.assertEqual(receipt["status"], "verified")
                self.assertEqual(receipt["quality"], "unvalidated")
                self.assertEqual(receipt["privacy"], "incomplete")
                self.assertTrue(receipt["risk_override"])
                self.assertEqual(client.sharing, PRIVATE)
                _, archive = package_bytes(target)
                with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
                    self.assertEqual(set(bundle.namelist()), set(manifest["files"]))
                    self.assertNotIn("evidence.md", bundle.namelist())
                    for name in bundle.namelist():
                        self.assertEqual(bundle.read(name), files[name])
                        self.assertIn(b"UNVALIDATED PRIVATE DRAFT", bundle.read(name))
                self.assertFalse((job / "generation").exists())
                recovery_snapshot(job, identifier)

    def test_changed_files_snapshot_audience_and_forged_normal_approval_are_rejected(self):
        identifier = create_recovery(self.root, "agent", "draft_structure_invalid")
        target = self.root / "package"
        for audience in ("local", "team:", "team:../other", None):
            with self.assertRaises(ValueError):
                prepare_recovery_package(self.root, identifier, target, audience, True)
        with self.assertRaises(ValueError):
            prepare_recovery_package(self.root, "0" * 64, target, "root", True)
        manifest = prepare_recovery_package(self.root, identifier, target, "root", True)
        approve_package(target, manifest["package_id"], [], confirmed_publish=True, accept_unvalidated=True)
        approval_path = target / "approval.json"
        approval = json.loads(approval_path.read_bytes())
        del approval["accept_unvalidated"]
        write_json(approval_path, approval)
        with self.assertRaisesRegex(ValueError, "override"):
            package_bytes(target)
        approve_package(target, manifest["package_id"], [], confirmed_publish=True, accept_unvalidated=True)
        (target / "files/agent-spec.md").write_text("Tampered", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "bytes changed"):
            package_bytes(target)

    def test_unvalidated_package_cannot_masquerade_as_a_validated_package(self):
        identifier = create_recovery(self.root, "both", "draft_quality_invalid")
        target = self.root / "package"
        manifest = prepare_recovery_package(self.root, identifier, target, "root", True)
        for field, value in (("quality", "pass"), ("privacy", "complete"), ("failure_code", "unknown"), ("recovery_id", "wrong")):
            changed = {**manifest, field: value}
            changed["package_id"] = digest({key: item for key, item in changed.items() if key != "package_id"})
            write_json(target / "manifest.json", changed)
            with self.assertRaisesRegex(ValueError, "unvalidated"):
                load_package(target)

    def test_native_retry_preserves_failed_attempt_and_publication_needs_explicit_risk_consent(self):
        bridge = NativeBridge("11111111-1111-4111-8111-111111111111", self.root / "native")
        try:
            source = bridge.capture([{"type": "session.start", "data": {"sessionId": bridge.session_id}},
                                     {"type": "user.message", "data": {"content": "Synthetic source"}}], "full")
            request = {"session": source["source"], "pipeline": PIPELINE, "readers": "both", "delivery": "local", "audience": "local",
                       "privacy_mode": "full", "detection": "none", "semantic": False}

            def failed(source, home, review_directory, *arguments, **settings):
                support = review_directory.parent / "abstraction/story/_support"
                support.mkdir(parents=True)
                write_json(support / "fast-candidate-0.json", self.candidate)
                raise PrivacyReviewFailure("PRIVATE_DIAGNOSTIC")

            with patch("session_spec.native_bridge.prepare_abstract_review", side_effect=failed):
                scanned = bridge.call("scan", request)
                bridge.studio.drain()
            failed_job = bridge.studio.job(scanned["job"])
            state = (failed_job["directory"] / "job.json").read_bytes()
            for change in ({"readers": "agent"}, {"audience": "root", "delivery": "artifactstore"}, {"retry_model": "--bad"}):
                with self.assertRaises(ValueError):
                    bridge.call("scan", {**request, "retry_of": scanned["job"], "retry_model": "synthetic-second", **change})
            bridge.studio.settings["model"] = "synthetic-configured"
            with mocked_abstraction() as drafting:
                retried = bridge.call("scan", {**request, "retry_of": scanned["job"], "retry_model": "synthetic-second"})
                bridge.studio.drain()
            self.assertEqual(drafting.call_args.kwargs["model"], "synthetic-second")
            self.assertNotEqual(retried["job"], scanned["job"])
            self.assertEqual((failed_job["directory"] / "job.json").read_bytes(), state)
            self.assertEqual(bridge.studio.settings["model"], "synthetic-configured")
            delivered = bridge.call("deliverables", scanned)
            package_request = {**scanned, "snapshot_id": delivered["snapshot_id"], "audience": "root", "delivery": "artifactstore"}
            with self.assertRaises(ValueError):
                bridge.call("recovery-package", package_request)
            manifest = bridge.call("recovery-package", {**package_request, "accept_unvalidated": True})
            with self.assertRaises(ValueError):
                bridge.call("recovery-package", {**package_request, "accept_unvalidated": True, "audience": "team:elsewhere"})
            client = Client({name: (failed_job["package"] / "files" / name).read_bytes() for name in manifest["files"]})
            with patch("session_spec.studio.ArtifactClient", return_value=client):
                plan = bridge.call("plan", {**scanned, "site": "synthetic-draft"})
                publish_request = {**scanned, "confirm": plan["plan_id"], "package_id": manifest["package_id"], "acknowledged": [], "publish_intent": True}
                with self.assertRaises(ValueError):
                    bridge.call("publish", publish_request)
                self.assertEqual(client.writes, [])
                bridge.call("publish", {**publish_request, "accept_unvalidated": True})
                bridge.studio.drain()
                with self.assertRaisesRegex(ValueError, "already attempted"):
                    bridge.call("publish", {**publish_request, "accept_unvalidated": True})
            self.assertEqual(bridge.call("status", scanned)["publication"]["quality"], "unvalidated")
            self.assertEqual(bridge.call("deliverables", scanned)["kind"], "unvalidated_draft")
            self.assertEqual(failed_job["recovery_cause"], "draft_privacy_invalid")
            self.assertNotIn("generation", failed_job)
        finally:
            bridge.close()


if __name__ == "__main__":
    unittest.main()
