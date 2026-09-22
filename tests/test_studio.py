import json
import shutil
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from session_spec.reduction import apply_review, recommended_decisions, scan_session
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from session_spec.studio import Studio, handler_for
from session_spec.delivery import deliver, validate_delivery


class StudioTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        source = self.root / "source/events.jsonl"
        source.parent.mkdir()
        source.write_text(json.dumps({"type": "user.message", "data": {"content": "Contact fake@example.invalid"}}), encoding="utf-8")
        self.studio = Studio(self.root / "home", self.root / "studio", str(source))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(self.studio))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temporary.cleanup()

    def request(self, path, headers=None, data=None):
        request = urllib.request.Request(self.url + path, data=data, headers=headers or {})
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, response.headers, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.headers, error.read()

    def test_no_token_wrong_origin_and_wrong_host_are_refused(self):
        self.assertEqual(self.request("/api/sessions")[0], 403)
        headers = {"Authorization": "Bearer " + self.studio.token}
        self.assertEqual(self.request("/api/sessions", {**headers, "Origin": "https://attacker.invalid"})[0], 403)
        self.assertEqual(self.request("/api/sessions", {**headers, "Host": "attacker.invalid"})[0], 403)
        status, response_headers, body = self.request("/api/sessions", headers)
        self.assertEqual(status, 200)
        self.assertNotIn(str(self.root).encode(), body)
        self.assertFalse(json.loads(body)["durable_jobs"])
        self.assertEqual(response_headers["Cache-Control"], "no-store")
        self.assertNotIn("Access-Control-Allow-Origin", response_headers)

    def test_static_page_never_contains_the_token(self):
        status, headers, body = self.request("/")
        self.assertEqual(status, 200)
        self.assertNotIn(self.studio.token.encode(), body)
        self.assertIn("script-src 'self';", headers["Content-Security-Policy"])
        self.assertIn(b' sandbox', body)

    def test_native_runtime_marks_its_jobs_as_durable(self):
        from session_spec.runtime import DurableStudio
        self.assertTrue(DurableStudio.durable_jobs)

    def test_three_step_ui_has_one_generate_action_and_no_review_checkboxes(self):
        _, _, body = self.request("/")
        for removed in (b'Save choices', b'I reviewed', b'I inspected', b'type="checkbox"', b'id="plan"', b'id="refresh-jobs"'):
            self.assertNotIn(removed, body)
        self.assertEqual(body.count(b'id="generate"'), 1)
        self.assertNotIn(b'id="step-share"', body)
        self.assertIn(b'<summary>Privacy & writing options', body)
        _, _, script = self.request("/studio.js")
        self.assertIn(b'confirmed: true', script)
        self.assertIn(b'publish_intent: true', script)
        self.assertNotIn(b'reviewed_all_files: true', script)

    @unittest.skipUnless(shutil.which("node"), "Node is required for UI state tests")
    def test_browser_state_transitions(self):
        result = subprocess.run(["node", "--test", str(Path(__file__).with_name("studio_ui.cjs"))],
                                capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_stale_destination_cannot_grant_upload_approval(self):
        package = self.root / "package"
        package.mkdir()
        self.studio.jobs["test"] = {"id": "test", "stage": "generate", "status": "done", "directory": self.root,
                                    "package": package, "plan": {"plan_id": "current"}}
        with patch("session_spec.studio.approve_package") as approve, patch("session_spec.studio.publish_package") as publish:
            with self.assertRaisesRegex(ValueError, "Destination changed"):
                self.studio.action("/api/publish", {"job": "test", "confirm": "stale", "publish_intent": True})
            approve.assert_not_called()
            publish.assert_not_called()

    def test_status_waits_for_terminal_checkpoint_write(self):
        entered = threading.Event()
        release = threading.Event()
        persisted = threading.Event()
        job = {"id": "test", "stage": "scan", "status": "new", "directory": self.root}
        self.studio.jobs["test"] = job

        def checkpoint(current):
            if current["status"] == "done":
                entered.set()
                release.wait(5)
                persisted.set()

        self.studio.changed = checkpoint
        self.studio.background(job, "scan", lambda: {"findings": 0})
        self.assertTrue(entered.wait(5))
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(self.request, "/api/status?job=test", {"Authorization": "Bearer " + self.studio.token})
            try:
                self.assertFalse(pending.done())
            finally:
                release.set()
            status, _, body = pending.result(timeout=5)
        self.assertTrue(persisted.is_set())
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["status"], "done")

    def test_preview_uses_srcdoc_without_relaxing_sandbox(self):
        status, headers, body = self.request('/')
        self.assertEqual(status, 200)
        self.assertRegex(body.decode(), r'<iframe id="human-preview"[^>]* sandbox>')
        self.assertNotIn(b'allow-scripts', body)
        self.assertNotIn(b'allow-same-origin', body)
        status, _, script = self.request('/studio.js')
        self.assertEqual(status, 200)
        self.assertIn(b"elements('human-preview').srcdoc = html", script)
        self.assertNotIn(b'let previewUrl', script)
        self.assertIn("frame-ancestors 'none'", headers['Content-Security-Policy'])

    def test_legacy_review_ui_does_not_offer_personal_bulk_removals(self):
        self.studio.jobs["test"] = {"id": "test", "status": "done", "directory": self.root}
        legacy = {"review_id": "legacy", "findings": [{
            "id": "personal", "category": "health", "text": "Private appointment",
            "recommended": "remove", "necessity": "unnecessary", "detectors": ["copilot"], "occurrences": [],
        }]}
        headers = {"Authorization": "Bearer " + self.studio.token}
        with patch("session_spec.studio.load_review", return_value=(legacy, [])):
            status, response_headers, body = self.request("/api/review?job=test", headers)
        self.assertEqual(status, 200)
        response = json.loads(body)
        self.assertEqual(response["review_id"], "legacy")
        self.assertIsNone(response["findings"][0]["recommended"])

    def test_unfinished_outputs_and_path_traversal_are_refused(self):
        self.studio.jobs["test"] = {"id": "test", "status": "running", "generation": self.root, "directory": self.root}
        headers = {"Authorization": "Bearer " + self.studio.token}
        for name in ("human-spec.html", "../../review.json", "_support/source.json"):
            self.assertEqual(self.request("/api/file?job=test&name=" + name, headers)[0], 400)
        status, _, _ = self.request("/api/scan", {**headers, "Content-Type": "text/plain"}, b"{}")
        self.assertEqual(status, 400)

    def test_review_endpoint_does_not_send_unverified_model_conclusions_or_rewrites(self):
        self.studio.jobs["test"] = {"id": "test", "status": "done", "directory": self.root}
        review = {"review_id": "synthetic-review", "findings": [{
            "id": "Psynthetic", "category": "inference", "text": "Exact synthetic source", "label": "PRIVATE_MODEL_GUESS",
            "reason": "PRIVATE_MODEL_GUESS", "alternative": "PRIVATE_MODEL_REWRITE", "necessity": "uncertain",
            "recommended": None, "detectors": ["copilot"], "occurrences": [], "assessments": [{"reason": "PRIVATE_MODEL_GUESS"}],
        }]}
        with patch("session_spec.studio.load_review", return_value=(review, [])):
            status, _, body = self.request("/api/review?job=test", {"Authorization": "Bearer " + self.studio.token})
        self.assertEqual(status, 200)
        self.assertNotIn(b"PRIVATE_MODEL_", body)
        returned = json.loads(body)
        self.assertEqual(returned["review_id"], "synthetic-review")
        self.assertEqual(returned["findings"][0]["text"], "Exact synthetic source")
        self.assertEqual(review["findings"][0]["reason"], "PRIVATE_MODEL_GUESS")

    def test_open_local_is_allowlisted_and_requires_finished_output(self):
        generation = self.root / "generation"
        (generation / "story").mkdir(parents=True)
        for name in ("human-spec.html", "agent-spec.md", "evidence.md"):
            (generation / "story" / name).write_text("# Handoff")
        selection = {"readers": "both", "destination": "local"}
        review = scan_session(self.studio.sessions[0]["path"], self.root / "home", self.root / "review", preferences=selection)
        apply_review(self.root / "review", recommended_decisions(review), generation / "reduced")
        deliver(generation, selection)
        self.studio.jobs["test"] = {"id": "test", "status": "done", "generation": generation, "directory": self.root}
        with patch("session_spec.studio.open_local") as opening:
            for name in ("../../secret.txt", "events.jsonl", "script.py"):
                with self.assertRaises(ValueError):
                    self.studio.action("/api/open", {"job": "test", "name": name})
            self.studio.action("/api/open", {"job": "test", "name": "agent-spec.md"})
            opening.assert_called_once_with(generation / "deliverables/agent-spec.md")

    def test_reopen_requires_matching_choices_and_validated_files(self):
        review_directory = self.root / "private-review"
        selection = {"readers": "both", "destination": "local"}
        review = scan_session(self.studio.sessions[0]["path"], self.root / "home", review_directory, preferences=selection)
        choices = recommended_decisions(review)
        generation = self.root / "existing-generation"
        apply_review(review_directory, choices, generation / "reduced")
        (generation / "story").mkdir()
        for name in ("human-spec.html", "agent-spec.md", "evidence.md"):
            (generation / "story" / name).write_text("# Reviewed fixture")
        deliver(generation, selection)
        with patch("session_spec.studio.validate_story", return_value={"valid": True}):
            reopened = Studio(self.root / "home", self.root / "new-studio", self.studio.sessions[0]["path"], generation, review_directory)
            self.assertIsNotNone(reopened.resume_job)
            self.assertEqual(reopened.resume_choices, choices["choices"])
            self.assertEqual(reopened.jobs[reopened.resume_job]["approved_choices"], choices)
        with patch("session_spec.studio.validate_story", return_value={"valid": False}):
            with self.assertRaisesRegex(ValueError, "validated"):
                Studio(self.root / "home", self.root / "new-studio", self.studio.sessions[0]["path"], generation, review_directory)

    def test_explicit_options_and_privacy_confirmation_are_required(self):
        for data in ({"session": "selected"}, {"session": "selected", "readers": "both", "delivery": "local", "audience": "local"},
                     {"session": "selected", "readers": "both", "delivery": "local", "audience": "local", "detection": "copilot", "semantic": False}):
            with self.assertRaises(ValueError):
                self.studio.action("/api/scan", data)
        self.assertEqual(self.studio.jobs, {})
        self.studio.jobs["test"] = {"id": "test", "status": "done", "directory": self.root}
        with self.assertRaisesRegex(ValueError, "Confirm"):
            self.studio.action("/api/generate", {"job": "test"})

    def test_local_human_selection_blocks_agent_download_and_upload(self):
        selection = {"readers": "human", "destination": "local"}
        review = scan_session(self.studio.sessions[0]["path"], self.root / "home", self.root / "review", preferences=selection)
        generation = self.root / "generation"
        apply_review(self.root / "review", recommended_decisions(review), generation / "reduced")
        (generation / "story").mkdir()
        (generation / "story/human-spec.html").write_text("<h1>Human only</h1>")
        result = deliver(generation, selection)
        self.studio.jobs["test"] = {"id": "test", "stage": "generate", "status": "done", "generation": generation, "directory": self.root, "result": result}
        headers = {"Authorization": "Bearer " + self.studio.token}
        status, _, body = self.request("/api/status?job=test", headers)
        self.assertEqual(status, 200)
        headers["X-Delivery-Snapshot"] = json.loads(body)["delivery_result"]["snapshot"]["id"]
        self.assertEqual(self.request("/api/file?job=test&name=human-spec.html", headers)[0], 200)
        self.assertEqual(self.request("/api/file?job=test&name=deliverables.zip", headers)[0], 200)
        for name in ("agent-spec.md", "evidence.md", "../../review.json"):
            self.assertEqual(self.request("/api/file?job=test&name=" + name, headers)[0], 400)
        with self.assertRaisesRegex(ValueError, "local files"):
            self.studio.action("/api/package", {"job": "test"})

    def test_snapshot_refuses_external_same_job_generation_change_and_missing_binding(self):
        selection = {"readers": "agent", "destination": "local"}
        review = scan_session(self.studio.sessions[0]["path"], self.root / "home", self.root / "review", preferences=selection)
        generations = []
        for label in ("first", "second"):
            generation = self.root / label
            apply_review(self.root / "review", recommended_decisions(review), generation / "reduced")
            (generation / "story").mkdir()
            for name in ("agent-spec.md", "evidence.md"):
                (generation / "story" / name).write_text(label + name)
            generations.append((generation, deliver(generation, selection)))
        job = {"id": "test", "stage": "generate", "status": "done", "generation": generations[0][0], "directory": self.root, "result": generations[0][1]}
        self.studio.jobs["test"] = job
        headers = {"Authorization": "Bearer " + self.studio.token}
        self.assertEqual(self.request("/api/file?job=test&name=agent-spec.md", headers)[0], 400)
        status, _, body = self.request("/api/status?job=test", headers)
        self.assertEqual(status, 200)
        old = json.loads(body)["delivery_result"]["snapshot"]
        headers["X-Delivery-Snapshot"] = old["id"]
        self.assertEqual(self.request("/api/file?job=test&name=agent-spec.md", headers)[2], b"firstagent-spec.md")
        with self.studio.action_lock, self.studio.lock:
            job.update(generation=generations[1][0], result=generations[1][1])
        for name in ("evidence.md", "deliverables.zip"):
            self.assertEqual(self.request("/api/file?job=test&name=" + name, headers)[0], 400)
        status, _, body = self.request("/api/status?job=test", headers)
        current = json.loads(body)["delivery_result"]["snapshot"]
        self.assertNotEqual(current["id"], old["id"])
        headers["X-Delivery-Snapshot"] = current["id"]
        status, response_headers, body = self.request("/api/file?job=test&name=evidence.md", headers)
        self.assertEqual((status, body), (200, b"secondevidence.md"))
        self.assertEqual(response_headers["X-Delivery-Snapshot"], current["id"])

        entered, release, switch_started = threading.Event(), threading.Event(), threading.Event()

        def held_validation(*arguments):
            manifest = validate_delivery(*arguments)
            entered.set()
            release.wait(5)
            return manifest

        def switch_generation():
            switch_started.set()
            with self.studio.action_lock, self.studio.lock:
                job.update(generation=generations[0][0], result=generations[0][1])

        with patch("session_spec.studio.validate_delivery", side_effect=held_validation), ThreadPoolExecutor(max_workers=2) as executor:
            reading = executor.submit(self.request, "/api/file?job=test&name=evidence.md", headers)
            self.assertTrue(entered.wait(5))
            switching = executor.submit(switch_generation)
            try:
                self.assertTrue(switch_started.wait(5))
                self.assertFalse(switching.done())
            finally:
                release.set()
            self.assertEqual(reading.result(timeout=5)[2], b"secondevidence.md")
            switching.result(timeout=5)


if __name__ == "__main__":
    unittest.main()
