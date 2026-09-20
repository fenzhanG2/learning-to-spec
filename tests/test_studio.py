import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from session_spec.reduction import apply_review, recommended_decisions, scan_session
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from session_spec.studio import Studio, handler_for
from session_spec.delivery import deliver


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
        self.assertEqual(response_headers["Cache-Control"], "no-store")
        self.assertNotIn("Access-Control-Allow-Origin", response_headers)

    def test_static_page_never_contains_the_token(self):
        status, headers, body = self.request("/")
        self.assertEqual(status, 200)
        self.assertNotIn(self.studio.token.encode(), body)
        self.assertIn("script-src 'self';", headers["Content-Security-Policy"])
        self.assertIn(b' sandbox', body)

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
        deliver(generation, selection)
        self.studio.jobs["test"] = {"id": "test", "status": "done", "generation": generation, "directory": self.root}
        headers = {"Authorization": "Bearer " + self.studio.token}
        self.assertEqual(self.request("/api/file?job=test&name=human-spec.html", headers)[0], 200)
        self.assertEqual(self.request("/api/file?job=test&name=deliverables.zip", headers)[0], 200)
        for name in ("agent-spec.md", "evidence.md", "../../review.json"):
            self.assertEqual(self.request("/api/file?job=test&name=" + name, headers)[0], 400)
        with self.assertRaisesRegex(ValueError, "local files"):
            self.studio.action("/api/package", {"job": "test"})


if __name__ == "__main__":
    unittest.main()
