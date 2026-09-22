import json
import copy
import io
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from session_spec.native_bridge import PIPELINE, NativeBridge, open_local_output, protocol_output, windows_markdown_handler
from native_abstract_fixture import mocked_abstraction
from offline_provider import guard_offline_test
from session_spec.runtime import FileLease
from session_spec.delivery import deliver
from session_spec.reduction import apply_review, digest, recommended_decisions, scan_session
from session_spec.ingest import origin_of, read_session
from session_spec.story_context import root_packet


SESSION = "11111111-1111-4111-8111-111111111111"


class NativeBridgeTests(unittest.TestCase):
    def setUp(self):
        guard_offline_test(self)
        self.temporary = tempfile.TemporaryDirectory()
        self.bridge = NativeBridge(SESSION, Path(self.temporary.name))
        self.events = [{"type": "session.start", "data": {"sessionId": SESSION}},
                       {"type": "user.message", "data": {"content": "Fix the CI matrix", "reasoningText": "HIDDEN"}}]

    def tearDown(self):
        self.bridge.close()
        self.temporary.cleanup()

    def test_capture_is_immutable_session_bound_and_never_discovers_other_sessions(self):
        with patch("session_spec.ingest.metadata_rows", side_effect=AssertionError("No metadata reads")), \
                patch("session_spec.ingest.list_sessions", side_effect=AssertionError("No discovery")):
            result = self.bridge.capture(self.events)
        self.assertEqual(result["session_id"], SESSION)
        source = Path(self.bridge.studio.sessions[0]["path"])
        self.assertNotIn("HIDDEN", source.read_text(encoding="utf-8"))
        self.assertEqual(self.events[1]["data"]["reasoningText"], "HIDDEN")
        self.assertNotIn("url", result)
        self.assertNotIn("token", result)
        self.assertNotIn(self.bridge.studio.token, json.dumps(result))
        self.assertIsNone(self.bridge.server)
        receipt = json.loads((source.parent / "capture.json").read_bytes())
        self.assertEqual(receipt["hard_removals"], {"secret_fields": 0, "secret_pattern_matches": 0})
        self.assertEqual(receipt["schema"], "native-session-capture/v2")
        self.assertEqual(receipt["capture_order"], PIPELINE)

    def test_wrong_session_and_invalid_records_are_refused(self):
        self.events[0]["data"]["sessionId"] = "22222222-2222-4222-8222-222222222222"
        with self.assertRaises(ValueError):
            self.bridge.capture(self.events)
        for events in ([], "source", [42], [{"data": "invalid"}]):
            with self.subTest(events=events), self.assertRaises(ValueError):
                self.bridge.capture(events)

    def test_scan_cannot_select_another_session_or_path(self):
        for selector in ("../events.jsonl", "other-session"):
            with self.assertRaises(ValueError):
                self.bridge.call("scan", {"session": selector})

    def test_observable_secrets_survive_for_private_abstraction_but_hidden_reasoning_does_not(self):
        self.events.append({"type": "assistant.message", "data": {"content": "ghp_" + "A" * 30}})
        self.events.append({"type": "assistant.reasoning", "data": {"content": "PRIVATE_REASONING"}})
        self.bridge.capture(self.events)
        source = Path(self.bridge.studio.sessions[0]["path"]).read_text(encoding="utf-8")
        self.assertIn("ghp_" + "A" * 30, source)
        self.assertNotIn("PRIVATE_REASONING", source)

    def test_browser_is_not_opened_by_capture(self):
        with patch("session_spec.studio.open_local", side_effect=AssertionError("External browser forbidden")):
            self.bridge.capture(self.events)

    def test_missing_approval_still_blocks_generation(self):
        captured = self.bridge.capture(self.events, "full")
        with mocked_abstraction() as drafting:
            result = self.bridge.call("scan", {"session": captured["source"], "readers": "both", "delivery": "local", "audience": "local", "privacy_mode": "full", "detection": "none", "semantic": False, "pipeline": PIPELINE})
            self.bridge.studio.drain()
        self.assertEqual(drafting.call_count, 1)
        self.assertEqual(self.bridge.call("status", result)["status"], "done")
        review = self.bridge.call("review", result)
        with self.assertRaises(ValueError):
            self.bridge.call("generate", {"job": result["job"], "pipeline": PIPELINE, "abstraction_sha256": review["abstraction_sha256"]})

    def test_positive_capture_keeps_observable_trajectory_not_private_ui_or_control_data(self):
        secret_url = "http://127.0.0.1:12345/#access=SYNTHETIC_CAPABILITY_NOT_FOR_EXPORT"
        additions = [
            {"type": "assistant.message", "privateEnvelope": "PRIVATE_ENVELOPE", "data": {
                "content": "The previous attempt failed; keep the correction.", "private": "PRIVATE_ASSISTANT_METADATA"}},
            {"type": "tool.execution_start", "id": "public-call-start", "data": {
                "toolName": "read_file", "toolCallId": "public-call", "arguments": {
                    "path": "synthetic.py", "private": "PRIVATE_ARGUMENT", "authorization": "OBSERVABLE_AUTH",
                    "strategy": {"mode": "incremental", "retry_budget": 3}, "custom_flag": True}}},
            {"type": "tool.execution_complete", "data": {"toolCallId": "public-call", "success": False,
                "result": {"content": "Expected acceptance check failed", "private": "PRIVATE_RESULT"},
                "error": {"message": "Mismatch remains", "private": "PRIVATE_ERROR"}}},
            {"type": "session.imported_context", "data": {"content": "Synthetic SWE-chat source context",
                "sourceTurnId": "source-turn", "sourceContext": {"source": "swe-chat", "role": "user", "private": "PRIVATE_CONTEXT"},
                "importMetadata": {"format": "swe-chat", "version": 1, "private": "PRIVATE_IMPORT"}}},
            {"type": "user.message", "data": {"content": "Correction: the acceptance failure must remain."}},
            {"type": "user.message", "data": {"content": "Private UI response", "source": "elicitation"}},
            {"type": "assistant.message", "data": {"content": "<canvas-context>PRIVATE_WRAPPER</canvas-context>"}},
            {"type": "tool.execution_start", "data": {"toolName": "ui_canvas", "toolCallId": "private-call", "arguments": {"content": "PRIVATE_CANVAS_ARGUMENT"}}},
            {"type": "tool.execution_complete", "data": {"toolCallId": "private-call", "result": {"content": "PRIVATE_CANVAS_RESULT"}}},
        ]
        for kind in ("elicitation.response", "ui.canvas", "system.message", "session.control", "future.private_event"):
            additions.append({"type": kind, "data": {"content": "PRIVATE_EVENT_PAYLOAD", "url": secret_url}})
        self.bridge.capture(self.events + additions)
        source = Path(self.bridge.studio.sessions[0]["path"])
        text = source.read_text(encoding="utf-8")
        self.assertNotIn("PRIVATE_", text)
        self.assertNotIn("SYNTHETIC_CAPABILITY", text)
        captured = [json.loads(line) for line in text.splitlines()]
        self.assertEqual([event["type"] for event in captured], ["session.start", "user.message", "assistant.message",
                         "tool.execution_start", "tool.execution_complete", "session.imported_context", "user.message"])
        self.assertEqual(captured[3]["data"]["arguments"], {"path": "synthetic.py", "authorization": "OBSERVABLE_AUTH", "strategy": {"mode": "incremental", "retry_budget": 3}, "custom_flag": True})
        self.assertEqual(captured[4]["data"]["success"], False)
        self.assertEqual(captured[4]["data"]["error"], {"message": "Mismatch remains"})
        self.assertEqual(captured[5]["data"]["sourceContext"], {"source": "swe-chat", "role": "user"})
        self.assertIn("acceptance failure must remain", text)
        self.assertEqual(json.loads((source.parent / "capture.json").read_bytes())["capture_policy"], "observable-events/v1")

    def test_literal_urls_remain_observable_but_nested_private_controls_are_excluded(self):
        marker = "SYNTHETIC_CAPABILITY_NOT_FOR_EXPORT"
        urls = ["http://127.0.0.1:12345/#access=" + marker,
                "https://example.invalid/canvas/" + marker,
                "https://example.invalid/item?token=" + marker,
                "https:\\/\\/example.invalid/canvas/" + marker,
                "https://example.invalid/item%23access%3D" + marker]
        self.events.extend([
            {"type": "assistant.message", "data": {"content": "Observed technical explanation " + " ".join(urls)}},
            {"type": "tool.execution_complete", "data": {"toolName": "read_file", "toolCallId": "observed",
                "result": {"content": [{"type": "text", "text": "VISIBLE_RESULT"},
                                       {"type": "elicitation.response", "text": "PRIVATE_ELICITATION"},
                                       {"role": "system", "content": "PRIVATE_SYSTEM"}],
                           "canvas": {"url": urls[0]}, "elicitationResponse": {"content": "PRIVATE_RESPONSE"}}}},
        ])
        self.bridge.capture(self.events)
        payload = Path(self.bridge.studio.sessions[0]["path"]).read_text(encoding="utf-8")
        self.assertIn(marker, payload)
        self.assertEqual(json.loads(payload.splitlines()[2])["data"]["content"], "Observed technical explanation " + " ".join(urls))
        self.assertNotIn("PRIVATE_ELICITATION", payload)
        self.assertNotIn("PRIVATE_SYSTEM", payload)
        self.assertNotIn("PRIVATE_RESPONSE", payload)
        self.assertIn("VISIBLE_RESULT", payload)
        self.assertIn("Observed technical explanation", payload)

    def test_uuid_binding_covers_all_event_envelopes_and_requires_a_matching_start(self):
        other = "22222222-2222-4222-8222-222222222222"
        for events in (self.events[1:], [{"type": "session.start", "data": {}}, self.events[1]],
                       self.events + [{"type": "assistant.message", "sessionId": other, "data": {"content": "wrong owner"}}],
                       self.events + [{"type": "unknown", "data": {"sessionId": other}}]):
            with self.subTest(events=events), self.assertRaises(ValueError):
                self.bridge.capture(events)
        self.assertFalse((self.bridge.root / "snapshots").exists())
        upper = "ABCDEFAB-1234-4567-89AB-ABCDEFABCDEF"
        with tempfile.TemporaryDirectory() as temporary:
            bridge = NativeBridge(upper, temporary)
            try:
                result = bridge.capture([{"type": "session.start", "data": {"sessionId": upper.lower()}}, self.events[1]])
                self.assertEqual(result["session_id"], upper.lower())
            finally:
                bridge.close()
        for value in ("1" * 36, "1" * 32, "not-a-session", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                NativeBridge(value, self.bridge.root / "invalid")

    def test_constructor_failures_release_lease_and_bound_socket(self):
        for target in ("NativeStudio",):
            root = self.bridge.root / target
            with self.subTest(target=target), patch("session_spec.native_bridge." + target, side_effect=RuntimeError("Synthetic constructor failure")):
                with self.assertRaises(RuntimeError):
                    NativeBridge(SESSION, root)
            with FileLease(root / "native.lock"):
                pass

    def test_close_keeps_lease_until_worker_and_terminal_checkpoint_settle(self):
        entered, release, closed = threading.Event(), threading.Event(), threading.Event()
        job = {"id": "a" * 32, "directory": self.bridge.root / "jobs" / ("a" * 32), "status": "new", "stage": "generate"}
        job["directory"].mkdir()
        self.bridge.studio.jobs[job["id"]] = job

        def operation():
            entered.set()
            if not release.wait(5):
                raise RuntimeError("Synthetic worker was not released")
            return {"synthetic": True}

        self.bridge.studio.background(job, "generate", operation)
        self.assertTrue(entered.wait(2))

        def closing():
            self.bridge.close()
            closed.set()

        closer = threading.Thread(target=closing)
        closer.start()
        try:
            for attempt in range(100):
                if getattr(self.bridge.studio, "closing", False):
                    break
                time.sleep(0.01)
            self.assertTrue(self.bridge.studio.closing)
            self.assertFalse(closed.is_set())
            with self.assertRaises(ValueError):
                with FileLease(self.bridge.root / "native.lock"):
                    self.fail("Lease escaped before the worker finished")
            with self.assertRaisesRegex(ValueError, "closing"):
                self.bridge.capture(self.events)
            with self.assertRaisesRegex(ValueError, "closing"):
                self.bridge.studio.background(job, "generate", lambda: None)
        finally:
            release.set()
            closer.join(5)
        self.assertFalse(closer.is_alive())
        self.assertTrue(closed.is_set())
        self.assertEqual(json.loads((job["directory"] / "job.json").read_bytes())["status"], "done")
        self.assertFalse(any(worker.is_alive() for worker in self.bridge.studio.workers))
        with FileLease(self.bridge.root / "native.lock"):
            pass
        self.bridge.close()

    def test_progress_prints_and_descriptor_writes_never_share_json_protocol_stdout(self):
        script = '''
import os, pathlib, sys, time
sys.path.insert(0, sys.argv[1])
sys.path.insert(0, str(pathlib.Path(sys.argv[1]) / "tests"))
from offline_provider import forbid_live_provider
from session_spec import cli, native_bridge
cli.local_defaults = lambda: {}
class SyntheticBridge(native_bridge.NativeBridge):
    def __init__(self, session_id, settings=None):
        print("CONSTRUCTOR_PROGRESS")
        super().__init__(session_id, root=root, settings=settings)
    def call(self, operation, data):
        if operation == "fail":
            raise ValueError("ghp_" + "Q" * 30)
        job = {"id": "a" * 32, "directory": self.root / "jobs" / ("a" * 32), "stage": "scan", "status": "new"}
        job["directory"].mkdir()
        self.studio.jobs[job["id"]] = job
        def work():
            print("WORKER_PROGRESS")
            os.write(1, b"DESCRIPTOR_PROGRESS\\n")
            time.sleep(0.1)
            print("WORKER_FINISHED")
            return {"synthetic": True}
        return self.studio.background(job, "scan", work)
native_bridge.NativeBridge = SyntheticBridge
root = sys.argv[2]
sys.argv = ["bridge", "--session", "11111111-1111-4111-8111-111111111111"]
with forbid_live_provider():
    native_bridge.main()
'''
        root = self.bridge.root / "protocol-process"
        result = subprocess.run([sys.executable, "-I", "-B", "-S", "-c", script, str(Path(__file__).resolve().parents[1]), str(root)],
                                input='{"id":1,"operation":"work"}\n{"id":2,"operation":"fail"}\n',
                                capture_output=True, text=True, encoding="utf-8", timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        responses = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual([response["id"] for response in responses], [1, 2])
        self.assertIn("result", responses[0])
        self.assertIn("error", responses[1])
        self.assertNotIn("ghp_", result.stdout + result.stderr)
        for marker in ("CONSTRUCTOR_PROGRESS", "WORKER_PROGRESS", "DESCRIPTOR_PROGRESS", "WORKER_FINISHED"):
            self.assertIn(marker, result.stderr)
            self.assertNotIn(marker, result.stdout)
        self.assertEqual(json.loads((root / "jobs" / ("a" * 32) / "job.json").read_bytes())["status"], "done")
        with FileLease(root / "native.lock"):
            pass

    def test_protocol_stream_restores_after_exception_without_file_descriptors(self):
        output, diagnostics = io.StringIO(), io.StringIO()
        with patch("sys.stdout", output), patch("sys.stderr", diagnostics):
            with self.assertRaisesRegex(RuntimeError, "synthetic"):
                with protocol_output() as responses:
                    print("diagnostic only")
                    responses.write('{"id":1}\n')
                    raise RuntimeError("synthetic")
            self.assertIs(sys.stdout, output)
        self.assertEqual(output.getvalue(), '{"id":1}\n')
        self.assertEqual(diagnostics.getvalue(), "diagnostic only\n")

    def test_worker_start_or_checkpoint_failure_does_not_leave_a_phantom_running_job(self):
        for failing in ("changed", "thread"):
            job = {"id": failing, "directory": self.bridge.root / "jobs" / failing, "status": "new", "stage": "generate"}
            job["directory"].mkdir()
            self.bridge.studio.jobs[failing] = job
            target = patch.object(self.bridge.studio, "changed", side_effect=OSError("Synthetic checkpoint failure")) if failing == "changed" else \
                patch("session_spec.native_bridge.threading.Thread.start", side_effect=RuntimeError("Synthetic worker start failure"))
            with self.subTest(failing=failing), target, self.assertRaises((OSError, RuntimeError)):
                self.bridge.studio.background(job, "generate", lambda: self.fail("Failed startup must not run an operation"))
            self.assertEqual(job["status"], "error")
        self.bridge.close()
        with FileLease(self.bridge.root / "native.lock"):
            pass

    def completed_output(self):
        self.bridge.capture(self.events)
        identifier = "c" * 32
        directory = self.bridge.root / "jobs" / identifier
        directory.mkdir()
        selection = {"readers": "agent", "destination": "local"}
        review = scan_session(self.bridge.studio.sessions[0]["path"], self.bridge.studio.home, directory / "review", preferences=selection)
        choices = recommended_decisions(review)
        generation = directory / "generation"
        apply_review(directory / "review", choices, generation / "reduced")
        (generation / "story").mkdir()
        for name in ("agent-spec.md", "evidence.md"):
            (generation / "story" / name).write_text("Synthetic selected " + name, encoding="utf-8")
        result = deliver(generation, selection)
        job = {"id": identifier, "directory": directory, "generation": generation, "stage": "generate", "status": "done",
               "approved_choices": choices, "decision_id": digest(choices), "result": result}
        self.bridge.studio.jobs[identifier] = job
        return job

    def preview_request(self, preview, route, token=None, method="GET", extra_headers=None, payload=None):
        url = urllib.parse.urlsplit(preview["url"])
        capability = urllib.parse.parse_qs(url.fragment)["access"][0] if token is None else token
        headers = {"Authorization": "Bearer " + capability, **(extra_headers or {})}
        request = urllib.request.Request(f"http://{url.netloc}{route}", headers=headers, method=method, data=payload)
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                return response.status, response.headers, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.headers, error.read()

    def test_output_canvas_only_shares_selected_verified_files_not_studio_or_private_state(self):
        job = self.completed_output()
        self.assertIsNone(self.bridge.server)
        preview = self.bridge.call("output_canvas", {"job": job["id"]})
        self.assertTrue(preview["read_only"])
        self.assertNotIn(self.bridge.studio.token, json.dumps(preview))
        self.assertEqual({item["name"] for item in preview["files"]}, {"agent-spec.md", "evidence.md"})
        self.assertEqual(preview["bundle"]["name"], "deliverables.zip")
        status, headers, body = self.preview_request(preview, "/api/delivery")
        self.assertEqual(status, 200)
        policy = headers["Content-Security-Policy"]
        self.assertIn("style-src 'self' 'unsafe-inline'", policy)
        self.assertIn("script-src 'self';", policy)
        self.assertNotIn("script-src 'self' 'unsafe-inline'", policy)
        for directive in ("default-src 'none'", "connect-src 'self'", "img-src 'self' data:",
                          "frame-src 'self' blob:", "object-src 'none'", "base-uri 'none'", "form-action 'none'"):
            self.assertIn(directive, policy)
        manifest = json.loads(body)
        self.assertEqual(manifest["snapshot_id"], preview["snapshot_id"])
        self.assertNotIn("local", preview)
        self.assertEqual(manifest["local"]["folder"], str(job["generation"] / "deliverables"))
        self.assertEqual(set(manifest["local"]["files"]), {"agent-spec.md", "evidence.md", "deliverables.zip"})
        import hashlib
        for item in [*manifest["files"], manifest["bundle"]]:
            status, headers, content = self.preview_request(preview, "/api/file?name=" + item["name"])
            self.assertEqual(status, 200)
            self.assertEqual(len(content), item["bytes"])
            self.assertEqual(hashlib.sha256(content).hexdigest(), item["sha256"])
            self.assertEqual(headers["X-Content-SHA256"], item["sha256"])
            self.assertEqual(headers["X-Delivery-Snapshot"], preview["snapshot_id"])
        for route in ("/api/review", "/api/status", "/api/sessions", "/studio.js", "/agent-spec.md", "/evidence.md", "/api/file?name=review.json",
                      "/api/file?name=../../review/review.json", "/api/file?name=human-spec.html", "/api/file?name=agent-spec.md&job=other"):
            self.assertNotEqual(self.preview_request(preview, route)[0], 200, route)
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            self.assertEqual(self.preview_request(preview, "/api/generate", method=method)[0], 405)
        self.assertEqual(self.preview_request(preview, "/api/delivery", token=self.bridge.studio.token)[0], 403)
        self.assertEqual(self.preview_request(preview, "/api/delivery", extra_headers={"Origin": "https://example.invalid"})[0], 403)
        static = self.bridge.root / "synthetic-web"
        static.mkdir()
        for name in ("native-output.html", "native-output.js", "native-output.css"):
            (static / name).write_text("Synthetic static file", encoding="utf-8")
        with patch("session_spec.native_bridge.WEB", static):
            status, _, body = self.preview_request(preview, "/", token="")
            self.assertEqual(status, 200)
            self.assertNotIn(urllib.parse.urlsplit(preview["url"]).fragment.encode(), body)

    def test_live_progress_is_authenticated_read_only_and_binds_only_completed_outputs(self):
        job = self.completed_output()
        job.update(stage="scan", status="running", automated_elapsed_seconds=42,
                   error="PRIVATE_SOURCE", history=[])
        preview = self.bridge.call("progress_canvas", {"job": job["id"]})
        self.assertTrue(preview["read_only"])
        self.assertEqual(self.bridge.progress_canvas(job["id"]), preview)
        status, _, body = self.preview_request(preview, "/api/progress")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"schema": "native-progress/v1", "phase": "draft",
                                          "elapsed_seconds": 42, "limit_seconds": 300})
        for route in ("/api/delivery", "/api/file?name=agent-spec.md"):
            self.assertEqual(self.preview_request(preview, route)[0], 409)
        for headers in ({"Origin": "https://example.invalid"}, {"Host": "example.invalid"}):
            self.assertEqual(self.preview_request(preview, "/api/progress", extra_headers=headers)[0], 403)
        self.assertEqual(self.preview_request(preview, "/api/progress", token="wrong")[0], 403)
        self.assertEqual(self.preview_request(preview, "/api/progress", method="POST")[0], 405)
        job.update(status="done")
        self.assertEqual(json.loads(self.preview_request(preview, "/api/progress")[2])["phase"], "review")
        self.assertEqual(self.preview_request(preview, "/api/delivery")[0], 409)
        job.update(stage="generate", status="done")
        self.assertEqual(json.loads(self.preview_request(preview, "/api/progress")[2])["phase"], "ready")
        self.assertEqual(self.bridge.output_canvas(job["id"])["url"], preview["url"])
        self.assertEqual(self.preview_request(preview, "/api/file?name=agent-spec.md")[0], 409)
        self.assertEqual(self.preview_request(preview, "/api/delivery")[0], 200)
        self.assertEqual(self.preview_request(preview, "/api/file?name=agent-spec.md")[0], 200)
        job.update(status="error", error_code="PRIVATE_FAILURE")
        status, _, body = self.preview_request(preview, "/api/progress")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["error_code"], "export_failed")
        self.assertNotIn("PRIVATE", body.decode())

    def test_output_preview_revalidates_changes_limits_and_rotates_readonly_capability(self):
        job = self.completed_output()
        job["status"] = "running"
        with self.assertRaises(ValueError):
            self.bridge.call("output_canvas", {"job": job["id"]})
        self.assertIsNone(self.bridge.server)
        job["status"] = "done"
        with patch("session_spec.native_bridge.LIMIT", 1), self.assertRaises(ValueError):
            self.bridge.output_canvas(job["id"])
        self.assertIsNone(self.bridge.server)
        first = self.bridge.output_canvas(job["id"])
        old_token = urllib.parse.parse_qs(urllib.parse.urlsplit(first["url"]).fragment)["access"][0]
        first_thread = self.bridge.thread
        second = self.bridge.output_canvas(job["id"])
        self.assertFalse(first_thread.is_alive())
        self.assertNotEqual(first["url"], second["url"])
        self.assertEqual(self.preview_request(second, "/api/delivery", token=old_token)[0], 403)
        decision_id = job["decision_id"]
        job["decision_id"] = "different"
        self.assertEqual(self.preview_request(second, "/api/delivery")[0], 409)
        self.assertEqual(self.preview_request(second, "/api/file?name=agent-spec.md")[0], 409)

        job["decision_id"] = decision_id
        original = job["generation"]
        replacement = original.with_name("replacement-generation")
        shutil.copytree(original, replacement)
        job["generation"] = replacement
        self.assertEqual(self.preview_request(second, "/api/delivery")[0], 409)
        self.assertEqual(self.preview_request(second, "/api/file?name=evidence.md")[0], 409)
        job["generation"] = original
        (job["generation"] / "deliverables/agent-spec.md").write_text("Changed bytes", encoding="utf-8")
        self.assertEqual(self.preview_request(second, "/api/file?name=agent-spec.md")[0], 409)

    def test_open_output_is_user_only_selected_snapshot_scoped_and_revalidated(self):
        job = self.completed_output()
        preview = self.bridge.output_canvas(job["id"])
        origin = "http://" + urllib.parse.urlsplit(preview["url"]).netloc
        headers = {"Origin": origin, "Content-Type": "application/json"}

        def request(target, **overrides):
            payload = json.dumps({"target": target, "snapshot_id": preview["snapshot_id"]}).encode()
            return self.preview_request(preview, "/api/open", method="POST", extra_headers=headers, payload=payload, **overrides)

        with patch("session_spec.native_bridge.open_local_output") as launch:
            for target in ("agent-spec.md", "evidence.md", "folder"):
                status, _, body = request(target)
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(body), {"status": "dispatched", "target": target})
                expected = job["generation"] / "deliverables"
                launch.assert_called_with(expected if target == "folder" else expected / target)
            launch.reset_mock()
            for target in ("human-spec.html", "deliverables.zip", "../review", "review.json", str(job["generation"]), "file:///etc/passwd", "https://example.invalid"):
                self.assertEqual(request(target)[0], 409)
            self.assertEqual(request("folder", token="wrong")[0], 403)
            self.assertNotEqual(self.preview_request(preview, "/api/open?target=folder")[0], 200)
            for header in ({}, {**headers, "Origin": "https://example.invalid"}, {**headers, "Host": "evil.invalid"}):
                self.assertEqual(self.preview_request(preview, "/api/open", method="POST", extra_headers=header,
                    payload=json.dumps({"target": "folder", "snapshot_id": preview["snapshot_id"]}).encode())[0], 403)
            for payload in (b"null", b"[]", b"{", b"{}", b"x" * 513,
                            json.dumps({"target": "folder", "snapshot_id": "wrong"}).encode(),
                            json.dumps({"target": "folder", "snapshot_id": preview["snapshot_id"], "path": "private"}).encode()):
                self.assertEqual(self.preview_request(preview, "/api/open", method="POST", extra_headers=headers, payload=payload)[0], 409)
            for method in ("PUT", "PATCH", "DELETE", "OPTIONS"):
                self.assertEqual(self.preview_request(preview, "/api/open", method=method, extra_headers=headers)[0], 405)
            job["status"] = "running"
            self.assertEqual(request("folder")[0], 409)
            job["status"] = "done"
            job["decision_id"] = "changed"
            self.assertEqual(request("folder")[0], 409)
            job["decision_id"] = digest(job["approved_choices"])
            (job["generation"] / "deliverables/agent-spec.md").write_text("Changed bytes", encoding="utf-8")
            self.assertEqual(request("agent-spec.md")[0], 409)
            self.assertEqual(request("folder")[0], 409)
            launch.assert_not_called()

    def test_open_requires_bound_snapshot_and_projects_launcher_failures_safely(self):
        job = self.completed_output()
        preview = self.bridge.progress_canvas(job["id"])
        origin = "http://" + urllib.parse.urlsplit(preview["url"]).netloc
        with patch("session_spec.native_bridge.open_local_output", side_effect=OSError("PRIVATE_HANDLER_PATH")) as launch:
            with self.assertRaises(ValueError):
                self.bridge.studio.open_output(job["id"], None, "folder")
            launch.assert_not_called()
            status, _, body = self.preview_request(preview, "/api/delivery")
            manifest = json.loads(body)
            status, _, body = self.preview_request(preview, "/api/open", method="POST",
                extra_headers={"Origin": origin, "Content-Type": "application/json"},
                payload=json.dumps({"target": "folder", "snapshot_id": manifest["snapshot_id"]}).encode())
            self.assertEqual(status, 409)
            self.assertNotIn(b"PRIVATE_HANDLER_PATH", body)

    def test_platform_openers_use_paths_not_shell_commands_and_bound_posix_wait(self):
        target = Path(self.temporary.name) / "report with spaces & punctuation.md"
        with patch("session_spec.native_bridge.sys.platform", "win32"), \
                patch("session_spec.native_bridge.windows_markdown_handler", return_value=True), \
                patch("session_spec.native_bridge.os.startfile", create=True) as start:
            open_local_output(target)
            start.assert_called_once_with(str(target), "open")
        missing_association = OSError("No associated app")
        missing_association.winerror = 1155
        with patch("session_spec.native_bridge.sys.platform", "win32"), \
                patch("session_spec.native_bridge.windows_markdown_handler", return_value=True), \
                patch("session_spec.native_bridge.os.startfile", create=True, side_effect=missing_association), \
                patch.dict("os.environ", {"WINDIR": str(Path(self.temporary.name) / "Windows")}), \
                patch("session_spec.native_bridge.subprocess.Popen") as launch:
            open_local_output(target)
            self.assertEqual(launch.call_args.args[0], [str(Path(self.temporary.name) / "Windows/System32/notepad.exe"), str(target)])
            self.assertFalse(launch.call_args.kwargs["shell"])
            with self.assertRaises(OSError):
                open_local_output(target.with_suffix(".html"))
            self.assertEqual(launch.call_count, 1)
        with patch("session_spec.native_bridge.sys.platform", "win32"), \
                patch("session_spec.native_bridge.windows_markdown_handler", return_value=False), \
                patch("session_spec.native_bridge.os.startfile", create=True) as start, \
                patch.dict("os.environ", {"WINDIR": str(Path(self.temporary.name) / "Windows")}), \
                patch("session_spec.native_bridge.subprocess.Popen") as launch:
            open_local_output(target)
            self.assertEqual(launch.call_args.args[0][-1], str(target))
            start.assert_not_called()
        for platform, command in (("darwin", "open"), ("linux", "xdg-open")):
            with patch("session_spec.native_bridge.sys.platform", platform), patch("session_spec.native_bridge.subprocess.run") as run:
                open_local_output(target)
                self.assertEqual(run.call_args.args[0], [command, str(target)])
                self.assertFalse(run.call_args.kwargs["shell"])
                self.assertEqual(run.call_args.kwargs["timeout"], 5)

    def test_markdown_association_probe_excludes_windows_picker_without_changing_settings(self):
        for result_code, executable, expected in ((0, "editor.exe", True), (0, "OpenWith.exe", False), (1, "", False), (0, "", False)):
            with patch("session_spec.native_bridge.ctypes.WinDLL", create=True) as library:
                def query(flags, kind, extension, verb, result, size):
                    self.assertEqual((flags, kind, extension, verb), (0, 2, ".md", "open"))
                    result.value = executable
                    return result_code
                library.return_value.AssocQueryStringW.side_effect = query
                self.assertEqual(windows_markdown_handler(), expected)
                library.assert_called_once_with("shlwapi")

    def test_open_refuses_reparse_paths_even_when_saved_bytes_are_valid(self):
        job = self.completed_output()
        preview = self.bridge.output_canvas(job["id"])
        folder = job["generation"] / "deliverables"
        original = Path.lstat

        def linked_stat(path, *args, **kwargs):
            value = original(path, *args, **kwargs)
            return SimpleNamespace(st_mode=value.st_mode, st_file_attributes=0x400) if path == folder else value

        with patch.object(Path, "lstat", linked_stat), patch("session_spec.native_bridge.open_local_output") as launch:
            for target in ("folder", "agent-spec.md"):
                with self.assertRaises(ValueError):
                    self.bridge.studio.open_output(job["id"], preview["snapshot_id"], target)
            launch.assert_not_called()

    def test_readonly_server_start_failure_closes_socket_without_releasing_bridge_lease(self):
        job = self.completed_output()
        servers = []
        from http.server import ThreadingHTTPServer

        def server_factory(*arguments):
            server = ThreadingHTTPServer(*arguments)
            servers.append(server)
            return server

        with patch("session_spec.native_bridge.ThreadingHTTPServer", side_effect=server_factory), \
                patch("session_spec.native_bridge.threading.Thread.start", side_effect=RuntimeError("Synthetic thread failure")):
            with self.assertRaises(RuntimeError):
                self.bridge.output_canvas(job["id"])
        self.assertEqual(servers[0].socket.fileno(), -1)
        self.assertIsNone(self.bridge.server)
        with self.assertRaises(ValueError):
            with FileLease(self.bridge.root / "native.lock"):
                self.fail("Live bridge lost its lease after preview failure")

    def test_capture_preserves_delegated_ownership_and_never_promotes_it_to_root(self):
        delegated = [
            {"type": "user.message", "agentId": "worker-one", "data": {"content": "DELEGATED agent request"}},
            {"type": "user.message", "parentToolCallId": "call-parent", "data": {"content": "DELEGATED tool request"}},
            {"type": "user.message", "parentAgentTaskId": "task-parent", "data": {"content": "DELEGATED task request"}},
            {"type": "user.message", "data": {"parentAgentTaskId": "data-task", "content": "DELEGATED nested task request"}},
            {"type": "user.message", "source": "agent-envelope", "data": {"content": "DELEGATED envelope source"}},
            {"type": "assistant.message", "data": {"source": "agent-build", "content": "DELEGATED assistant result"}},
            {"type": "user.message", "data": {"source": "skill-summary", "content": "INJECTED skill context"}},
        ]
        events = self.events + delegated
        original = copy.deepcopy(events)
        result = self.bridge.capture(events)
        source = Path(self.bridge.studio.sessions[0]["path"])
        captured = [json.loads(line) for line in source.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(result["user_messages"], 1)
        for index, field in ((2, "agentId"), (3, "parentToolCallId"), (4, "parentAgentTaskId"), (6, "source")):
            self.assertEqual(captured[index][field], events[index][field])
            self.assertEqual(captured[index]["data"][field], events[index][field])
        self.assertEqual(captured[4]["data"]["source"], "agent-task")
        self.assertEqual(captured[7]["data"]["source"], "agent-build")
        self.assertEqual([origin_of(event, SESSION) for event in captured[2:]], ["delegated"] * 6 + ["injected"])
        metadata, records = read_session(source, self.bridge.studio.home)
        root = root_packet(records)
        self.assertNotIn("DELEGATED", json.dumps(root))
        self.assertNotIn("INJECTED", json.dumps(root))
        self.assertIn("Fix the CI matrix", json.dumps(root))
        self.assertEqual(events, original)
        with self.assertRaisesRegex(ValueError, "root user"):
            self.bridge.capture([self.events[0], delegated[0]])
        self.assertEqual(len(self.bridge.studio.sessions), 1)

    def test_arbitrary_nested_tool_results_and_errors_survive_private_field_sanitization(self):
        result = {"type": "structured-result", "stats": {"filesChanged": 2, "latency_ms": 4},
                  "rows": [{"name": "alpha", "type": "controller", "controlFlow": "early return", "systemMetrics": {"free": 12},
                            "details": {"warnings": ["acceptance still fails"], "accepted": False}}],
                  "privateMetadata": {"secret": "PRIVATE_RESULT"},
                  "nested": {"count": 0, "unset": None, "access_token": "OBSERVABLE_TOKEN", "trace": ["first", None, "retry"]}}
        error = {"code": "ERR_SYNTHETIC", "retryable": False, "diagnostics": {"expected": 2, "observed": 1}, "private": "PRIVATE_ERROR"}
        event = {"type": "tool.execution_complete", "data": {"toolName": "custom_coding_tool", "toolCallId": "custom-call",
                 "success": True, "result": result, "error": error}}
        original = copy.deepcopy(event)
        self.bridge.capture(self.events + [event])
        source = Path(self.bridge.studio.sessions[0]["path"])
        text = source.read_text(encoding="utf-8")
        captured = json.loads(text.splitlines()[-1])["data"]
        expected_result = copy.deepcopy(result)
        del expected_result["privateMetadata"]
        self.assertEqual(captured["result"], expected_result)
        self.assertEqual(captured["error"], {key: value for key, value in error.items() if key != "private"})
        self.assertTrue(captured["success"])
        self.assertNotIn("PRIVATE_", text)
        from session_spec.privacy import content_redaction
        with content_redaction(False):
            metadata, records = read_session(source, self.bridge.studio.home)
        observed = next(record for record in records if record.get("tool") == "custom_coding_tool")
        self.assertEqual(observed["result"], expected_result)
        self.assertEqual(event, original)
