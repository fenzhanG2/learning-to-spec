import io
import hashlib
import json
import tempfile
import unittest
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from unittest.mock import patch

from session_spec.artifacts import ArtifactError, PRIVATE, destination_plan, publish_package, verify_file, verify_publication
from session_spec.reduction import digest
from session_spec.share_package import approve_package, load_package, package_bytes
from session_spec.storage import file_hash, write_json


class Client:
    def __init__(self, files, existing=False, team=None):
        self.files = files
        self.existing = existing
        self.team = team
        self.sharing = {"allAuthenticated": True}
        self.writes = []

    def request(self, method, path, data=None, raw=False):
        if method == "PUT":
            self.writes.append((method, path))
            self.sharing = data
            return {"ok": True}
        if path.startswith("/api/teams/"):
            return {"sharing": {"allAuthenticated": False, "groups": ["test-team"]}}
        if path.startswith("/api/sites/"):
            if not self.existing:
                raise ArtifactError(404)
            return {"sharing": self.sharing, "team": self.team, "files": list(self.files)}
        name = path.split("/")[-1].split("?")[0]
        return self.files[name]

    def upload(self, site, archive, team=None):
        self.writes.append(("UPLOAD", site, team, archive))
        self.existing = True
        return {"ok": True, "name": site}


class PrivateSharingTests(unittest.TestCase):
    def test_known_service_html_decorations_preserve_every_authored_byte(self):
        content = b'<html><head><title>Fixture</title></head><body>Approved story</body></html>'
        expected = hashlib.sha256(content).hexdigest()
        decorated = content.replace(b'<head>', b'<head><script src="/url-utils.js"></script><script src="/link-security.js"></script><script data-as-csrf src="/csrf-bootstrap.js"></script>')
        decorated = decorated.replace(b'</head>', b'\n<link data-artifactstore-favicon rel="icon" type="image/svg+xml" href="/favicon.svg?v=3.0.2">\n<link data-artifactstore-favicon rel="apple-touch-icon" href="/apple-touch-icon.png?v=3.0.2">\n</head>')
        decorated = decorated.replace(b'</body>', b'<script data-as-editor src="/_editor/widget.js" data-site="fixture" data-page="index.html"></script></body>')
        self.assertEqual(verify_file(content, expected, 'index.html', 'fixture')['method'], 'exact_bytes')
        self.assertEqual(verify_file(decorated, expected, 'index.html', 'fixture')['method'], 'authored_html_with_known_service_decorations')
        for changed in (decorated.replace(b'Approved story', b'Changed story'),
                        decorated.replace(b'/link-security.js', b'https://untrusted.invalid/link-security.js'),
                        decorated.replace(b'</body>', b'<script>unexpected()</script></body>')):
            with self.assertRaises(ValueError):
                verify_file(changed, expected, 'index.html', 'fixture')
        with self.assertRaises(ValueError):
            verify_file(decorated, expected, 'index.html', 'different-site')
        with self.assertRaises(ValueError):
            verify_file(decorated, expected, 'agent-spec.md', 'fixture')

    def test_reverification_is_read_only_and_checks_file_set_policy_and_plan(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest, files = self.fixture(root)
            approve_package(root, manifest['package_id'], [], True)
            client = Client(files)
            plan = destination_plan(root, 'fixture', client)
            publish_package(root, plan, plan['plan_id'], client)
            client.writes.clear()
            self.assertEqual(verify_publication(root, plan, client)['status'], 'verified')
            self.assertEqual(client.writes, [])
            with self.assertRaises(ValueError):
                verify_publication(root, {**plan, 'site': 'elsewhere'}, client)
            client.files['unexpected.json'] = b'not approved'
            with self.assertRaisesRegex(ValueError, 'file list'):
                verify_publication(root, plan, client)
            client.files.pop('unexpected.json')
            client.sharing = {'allAuthenticated': True}
            with self.assertRaisesRegex(ValueError, 'Owner-only'):
                verify_publication(root, plan, client)

    def fixture(self, root, audience="root", findings=None):
        files = {"index.html": b"<!doctype html><title>Reduced story</title>", "agent-spec.md": b"# Handoff\nKeep the failed test.", "evidence.md": b"# Evidence\nRecorded failure."}
        (root / "files").mkdir()
        for name, content in files.items():
            (root / "files" / name).write_bytes(content)
        manifest = {"schema": "share-package/v1", "audience": audience, "files": {name: file_hash(root / "files" / name) for name in files}, "findings": findings or []}
        manifest["package_id"] = digest(manifest)
        write_json(root / "manifest.json", manifest)
        return manifest, files

    def test_no_upload_without_exact_package_and_final_findings_approval(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest, files = self.fixture(root, findings=[{"id": "F1"}])
            with self.assertRaises(OSError):
                package_bytes(root)
            with self.assertRaises(ValueError):
                approve_package(root, manifest["package_id"], [], True)
            approve_package(root, manifest["package_id"], ["F1"], True)
            _, archive = package_bytes(root)
            with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
                self.assertEqual(set(zipped.namelist()), set(files))
            (root / "files/agent-spec.md").write_text("Changed after approval", encoding="utf-8")
            with self.assertRaises(ValueError):
                package_bytes(root)

    def test_extra_source_file_and_symlink_are_not_uploadable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root)
            (root / "files/source.json").write_text("private transcript path", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_package(root)

    def test_bytes_changed_between_validation_and_zip_are_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest, files = self.fixture(root)
            approve_package(root, manifest["package_id"], [], True)

            def changed(directory):
                result = load_package(directory)
                (root / "files/agent-spec.md").write_text("Unapproved replacement")
                return result

            with patch("session_spec.share_package.load_package", side_effect=changed):
                with self.assertRaisesRegex(ValueError, "while bundling"):
                    package_bytes(root)

    def test_agent_only_package_and_placeholder_never_add_html(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "files").mkdir()
            files = {"agent-spec.md": b"# Agent only", "evidence.md": b"# Evidence"}
            for name, content in files.items():
                (root / "files" / name).write_bytes(content)
            manifest = {"schema": "share-package/v2", "readers": "agent", "audience": "root", "files": {name: file_hash(root / "files" / name) for name in files}, "findings": []}
            manifest["package_id"] = digest(manifest)
            write_json(root / "manifest.json", manifest)
            approve_package(root, manifest["package_id"], [], True)
            client = Client(files)
            plan = destination_plan(root, "agent-only", client)
            self.assertEqual(publish_package(root, plan, plan["plan_id"], client)["status"], "verified")
            for upload in (client.writes[0], client.writes[2]):
                with zipfile.ZipFile(io.BytesIO(upload[3])) as archive:
                    self.assertNotIn("index.html", archive.namelist())

    def test_root_upload_privacy_is_verified_before_real_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest, files = self.fixture(root)
            approve_package(root, manifest["package_id"], [], True)
            client = Client(files)
            plan = destination_plan(root, "new-story", client)
            self.assertEqual(client.writes, [])
            result = publish_package(root, plan, plan["plan_id"], client)
            self.assertEqual(result["status"], "verified")
            self.assertTrue(result["files"]["agent-spec.md"].endswith("?raw=1"))
            self.assertTrue(result["files"]["evidence.md"].endswith("?raw=1"))
            self.assertEqual([write[0] for write in client.writes], ["UPLOAD", "PUT", "UPLOAD"])
            with zipfile.ZipFile(io.BytesIO(client.writes[0][3])) as archive:
                self.assertNotIn(b"Keep the failed test", archive.read("index.html"))
            self.assertEqual(client.sharing, PRIVATE)

    def test_existing_site_wrong_confirmation_and_changed_team_policy_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest, files = self.fixture(root, audience="team:reviewers")
            approve_package(root, manifest["package_id"], [], True)
            with self.assertRaisesRegex(ValueError, "exists"):
                destination_plan(root, "existing", Client(files, existing=True))
            client = Client(files, team="reviewers")
            plan = destination_plan(root, "new-story", client)
            with self.assertRaises(ValueError):
                publish_package(root, plan, "not-approved", client)
            changed = {**plan, "viewer_policy": {"allAuthenticated": True}}
            with self.assertRaises(ValueError):
                publish_package(root, changed, changed["plan_id"], client)
            self.assertEqual(client.writes, [])

    def test_missing_acl_proof_blocks_content_and_preserves_recovery_receipt(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest, files = self.fixture(root)
            approve_package(root, manifest["package_id"], [], True)
            client = Client(files)
            plan = destination_plan(root, "new-story", client)
            original = client.request

            def missing(method, path, data=None, raw=False):
                if method == "GET" and path.startswith("/api/sites/") and client.existing:
                    return {}
                return original(method, path, data, raw)

            client.request = missing
            with self.assertRaisesRegex(ValueError, "could not be verified"):
                publish_package(root, plan, plan["plan_id"], client)
            self.assertEqual(sum(write[0] == "UPLOAD" for write in client.writes), 1)
            self.assertEqual(json.loads((root / "publication.json").read_bytes())["status"], "placeholder_created")


if __name__ == "__main__":
    unittest.main()
