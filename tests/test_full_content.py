import json
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from session_spec.native_bridge import PIPELINE, NativeBridge
from native_abstract_fixture import mocked_abstraction
from offline_provider import guard_offline_test
from session_spec.privacy import content_redaction, redact_text, sanitize
from session_spec.private_cli import generate_private
from session_spec.reduction import apply_review, load_review, prepare_full_session, transform
from session_spec.agent_package import render_agent_package
from session_spec.pipeline import write_json
from session_spec.story_pipeline import render_story


SESSION = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"
SECRET = "ghp_" + "S" * 30


class FullContentTests(unittest.TestCase):
    def setUp(self):
        guard_offline_test(self)
        self.temporary = tempfile.TemporaryDirectory()
        self.bridge = NativeBridge(SESSION, Path(self.temporary.name) / "native")
        self.events = [
            {"type": "session.start", "data": {"sessionId": SESSION}},
            {"type": "user.message", "data": {"content": "Fixture contact alex@example.test with " + SECRET,
                                              "reasoningText": "HIDDEN_SYNTHETIC"}},
            {"type": "tool.execution_start", "data": {"toolName": "synthetic_request", "toolCallId": "call",
                                                        "arguments": {"api_key": SECRET, "private_key": SECRET,
                                                                      "private_key_file": "keys/synthetic.pem", "private_keys": [SECRET],
                                                                      "privateMetadata": "HIDDEN_SYNTHETIC",
                                                                      "url": "https://example.test/?token=" + SECRET}}},
            {"type": "assistant.reasoning", "data": {"content": "HIDDEN_SYNTHETIC"}},
        ]

    def tearDown(self):
        self.bridge.close()
        self.temporary.cleanup()

    def prepare(self, mode="full"):
        capture = self.bridge.call("capture", {"events": self.events, "privacy_mode": mode, "capture_order": PIPELINE})
        self.assertEqual(capture["privacy_mode"], mode)
        with mocked_abstraction() as drafting:
            result = self.bridge.call("scan", {"session": capture["source"], "readers": "both", "delivery": "local", "pipeline": PIPELINE,
                                         "audience": "local", "privacy_mode": mode,
                                         "detection": "none" if mode == "full" else "copilot", "semantic": mode == "llm"})
            self.bridge.studio.drain()
        self.assertEqual(drafting.call_count, 1)
        self.assertNotEqual(self.bridge.call("status", result)["status"], "running")
        return self.bridge.studio.job(result["job"])

    def test_full_capture_and_abstract_review_preserve_visible_content_without_detectors(self):
        with patch("session_spec.abstract_privacy.scan_session", side_effect=AssertionError("No rule scan")), \
                patch("session_spec.abstract_privacy.semantic_review", side_effect=AssertionError("No model scan")):
            job = self.prepare()
        self.assertEqual(job["status"], "done")
        review, baseline = load_review(job["directory"] / "review")
        self.assertEqual(review["privacy_mode"], "full")
        self.assertEqual(review["findings"], [])
        source = Path(self.bridge.studio.sessions[0]["path"])
        captured = [json.loads(line) for line in source.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(captured[2]["data"]["arguments"]["api_key"], SECRET)
        self.assertEqual(captured[2]["data"]["arguments"]["private_key"], SECRET)
        self.assertEqual(captured[2]["data"]["arguments"]["private_key_file"], "keys/synthetic.pem")
        self.assertEqual(captured[2]["data"]["arguments"]["private_keys"], [SECRET])
        self.assertEqual(baseline[0]["type"], "artifact.abstracted")
        self.assertIn("?token=" + SECRET, json.dumps(baseline))
        self.assertNotIn("HIDDEN_SYNTHETIC", json.dumps(baseline))
        decisions = {"review_id": review["review_id"], "audience": "local", "choices": {}}
        destination = job["directory"] / "full-copy"
        apply_review(job["directory"] / "review", decisions, destination)
        self.assertIn(SECRET, (destination / "events.jsonl").read_text(encoding="utf-8"))
        self.assertIn("alex@example.test", (destination / "events.jsonl").read_text(encoding="utf-8"))
        self.assertEqual(json.loads((destination / "events.jsonl").read_bytes()), baseline[0])

    def test_legacy_full_generation_scopes_content_mode_and_restores_default(self):
        self.bridge.capture(self.events, "full")
        directory = Path(self.temporary.name) / "legacy"
        prepare_full_session(self.bridge.studio.sessions[0]["path"], self.bridge.studio.home, directory / "review", "local",
                             {"readers": "both", "destination": "local"})
        review, baseline = load_review(directory / "review")
        decisions = {"review_id": review["review_id"], "audience": "local", "choices": {}}

        def story(*arguments, **settings):
            self.assertEqual(sanitize(SECRET), SECRET)
            self.assertNotIn(SECRET, redact_text(SECRET))
            self.assertIn(SECRET, (settings["from_export"] / "evidence.jsonl").read_text(encoding="utf-8"))
            return {"status": "synthetic"}

        with patch("session_spec.private_cli.run_story", side_effect=story), \
                patch("session_spec.private_cli.deliver", return_value={"files": []}):
            result = generate_private(directory / "review", decisions, directory / "generation",
                                      self.bridge.studio.home, confirm_choices=True)
        self.assertIn("No privacy scan", result["privacy"])
        self.assertNotIn(SECRET, sanitize(SECRET))

    @unittest.skipUnless(shutil.which("node"), "Node.js is needed for actual HTML rendering")
    def test_full_mode_preserves_visible_values_in_actual_html_markdown_and_evidence(self):
        from test_story_pipeline import article, insights, packet
        candidate, events = article(), packet()
        candidate["opening"] += " " + SECRET
        candidate["agent_markdown"] += "\nSynthetic visible value: " + SECRET + "\n"
        events[0]["text"] += " " + SECRET
        events[0]["human_input"] = events[0]["text"]
        directory = Path(self.temporary.name) / "render"
        directory.mkdir()
        with content_redaction(False):
            write_json(directory / "article.json", candidate)
            write_json(directory / "insights.json", insights())
            write_json(directory / "language.json", {"language": "en"})
            write_json(directory / "agent-presentation.json", {"schema": "agent-presentation/v2", "mode": "markdown-files"})
            render_story(directory, directory / "human-spec.html")
            outputs = render_agent_package(candidate, events, "en")
        self.assertIn(SECRET, (directory / "human-spec.html").read_text(encoding="utf-8"))
        self.assertIn(SECRET, outputs["agent-spec.md"])
        self.assertIn(SECRET, outputs["evidence.md"])
        self.assertNotIn(SECRET, sanitize(SECRET))

    def test_raw_scope_does_not_change_other_threads_or_error_redaction(self):
        results = []
        with content_redaction(False):
            self.assertEqual(sanitize({"api_key": SECRET})["api_key"], SECRET)
            worker = threading.Thread(target=lambda: results.append(sanitize(SECRET)))
            worker.start()
            worker.join()
            self.assertEqual(results, ["[REDACTED]"])
            self.assertEqual(redact_text(SECRET), "[REDACTED]")
        self.assertEqual(sanitize(SECRET), "[REDACTED]")
        with self.assertRaises(RuntimeError):
            with content_redaction(False):
                raise RuntimeError("synthetic exception")
        self.assertEqual(sanitize(SECRET), "[REDACTED]")

    def test_smart_failure_never_becomes_rules_only_success(self):
        with patch("session_spec.abstract_privacy.semantic_review", side_effect=ValueError("Synthetic LLM failure")):
            job = self.prepare("llm")
        self.assertEqual(job["status"], "error")
        review, baseline = load_review(job["directory"] / "review")
        self.assertNotIn(SECRET, json.dumps(baseline))
        with self.assertRaisesRegex(ValueError, "completed Copilot review"):
            transform(review, baseline, {"review_id": review["review_id"], "audience": "local", "choices": {}})
        self.assertEqual(self.bridge.call("pending_review", {}), {"review": None})

    def test_modes_cannot_change_after_capture_or_silently_skip_llm(self):
        capture = self.bridge.call("capture", {"events": self.events, "privacy_mode": "full", "capture_order": PIPELINE})
        base = {"session": capture["source"], "readers": "both", "delivery": "local", "audience": "local", "pipeline": PIPELINE}
        with self.assertRaisesRegex(ValueError, "mode changed"):
            self.bridge.call("scan", {**base, "privacy_mode": "llm", "detection": "copilot", "semantic": True})
        with self.assertRaisesRegex(ValueError, "explicitly skip"):
            self.bridge.call("scan", {**base, "privacy_mode": "full", "detection": "local", "semantic": False})
        capture = self.bridge.call("capture", {"events": self.events, "privacy_mode": "llm", "capture_order": PIPELINE})
        with self.assertRaisesRegex(ValueError, "explicit native privacy mode"):
            self.bridge.call("scan", {**base, "session": capture["source"], "detection": "local", "semantic": False})
        with self.assertRaisesRegex(ValueError, "both local rules and Copilot"):
            self.bridge.call("scan", {**base, "session": capture["source"], "privacy_mode": "llm", "detection": "local", "semantic": False})

    def test_saved_review_survives_restart_without_approval_or_new_source_read(self):
        job = self.prepare()
        root = self.bridge.root
        self.bridge.close()
        self.bridge = NativeBridge(SESSION, root)
        with patch("session_spec.ingest.metadata_rows", side_effect=AssertionError("No metadata reads")), \
                patch("session_spec.ingest.list_sessions", side_effect=AssertionError("No session discovery")):
            pending = self.bridge.call("pending_review", {})["review"]
        self.assertEqual(pending["job"], job["id"])
        self.assertEqual(pending["privacy_mode"], "full")
        self.assertNotIn(SECRET, json.dumps(pending))
        self.assertNotIn("source_path", pending)
        with self.assertRaises(ValueError):
            self.bridge.call("generate", {"job": job["id"]})
        with self.assertRaises(ValueError):
            self.bridge.call("pending_review", {"session": "other"})
        self.assertNotIn("generation", self.bridge.studio.job(job["id"]))

    def test_saved_review_refuses_changed_snapshot_identity_or_bytes(self):
        job = self.prepare()
        review, baseline = load_review(job["directory"] / "review")
        source = Path(self.bridge.studio.sessions[0]["path"])
        capture_path = source.parent / "capture.json"
        original = capture_path.read_bytes()
        binding = json.loads(original)
        binding["session_id"] = "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb"
        capture_path.write_text(json.dumps(binding), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "identity changed"):
            self.bridge.call("pending_review", {})
        with patch.object(self.bridge.studio, "action", side_effect=AssertionError("No generation worker")):
            with self.assertRaisesRegex(ValueError, "identity changed"):
                self.bridge.call("generate", {"job": job["id"], "review_id": review["review_id"], "choices": {}, "confirmed": True, "pipeline": PIPELINE, "abstraction_sha256": review["abstraction_sha256"]})
        binding["session_id"] = SESSION
        binding["privacy_mode"] = "llm"
        capture_path.write_text(json.dumps(binding), encoding="utf-8")
        with patch.object(self.bridge.studio, "action", side_effect=AssertionError("No generation worker")):
            with self.assertRaisesRegex(ValueError, "privacy mode does not match"):
                self.bridge.call("generate", {"job": job["id"], "review_id": review["review_id"], "choices": {}, "confirmed": True, "pipeline": PIPELINE, "abstraction_sha256": review["abstraction_sha256"]})
        capture_path.write_bytes(original)
        source.write_text(source.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "changed"):
            self.bridge.call("pending_review", {})


if __name__ == "__main__":
    unittest.main()
