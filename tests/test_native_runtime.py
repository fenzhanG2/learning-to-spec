import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from session_spec.delivery import deliver
from session_spec.mcp_server import Server, TOOLS, validate_call
from session_spec.reduction import apply_review, load_review
from session_spec.runtime import DurableStudio, FileLease, LocalClient, runtime_root


ROOT = Path(__file__).resolve().parents[1]


class NativeProtocolTests(unittest.TestCase):
    def test_discovery_has_typed_tools_but_no_approval_or_raw_read_shortcut(self):
        server = Server(client=object())
        response = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
        self.assertEqual(response["result"]["protocolVersion"], "2025-06-18")
        result = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]
        names = {item["name"] for item in result["tools"]}
        self.assertEqual(len(names), 12)
        self.assertTrue({"start_review", "generate", "deliverables", "verify_publish"} <= names)
        self.assertFalse({"approve", "publish", "raw_session", "read_file", "shell"} & names)
        self.assertTrue(all(item["inputSchema"]["additionalProperties"] is False for item in TOOLS))

    def test_validation_refuses_missing_unknown_wrong_types_and_paths(self):
        for name, arguments in [("generate", {}), ("generate", {"job": "../secret"}), ("health", {"confirmed": True}),
                                ("list_jobs", {"limit": True}), ("list_jobs", {"limit": 101}),
                                ("read_deliverable", {"job": "a" * 32, "name": "baseline.json"}),
                                ("start_review", {"session": "id"})]:
            with self.subTest(name=name, arguments=arguments), self.assertRaises(ValueError):
                validate_call(name, arguments)

    def test_exception_details_never_enter_mcp_response(self):
        class BrokenClient:
            def call(self, name, arguments, **options):
                raise ValueError("PRIVATE_EMAIL@example.invalid ghp_SENSITIVE")
        server = Server(BrokenClient())
        server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        response = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "health"}})
        self.assertTrue(response["result"]["isError"])
        self.assertNotIn("PRIVATE_EMAIL", json.dumps(response))
        self.assertNotIn("ghp_", json.dumps(response))

    def test_long_calls_emit_only_safe_protocol_progress(self):
        class SlowClient:
            def call(self, name, arguments, progress=None):
                progress(10, "Working locally")
                return {"status": "done"}
        server = Server(SlowClient())
        server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        notifications = []
        response = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
            "name": "generate", "arguments": {"job": "a" * 32}, "_meta": {"progressToken": "progress-1"}}}, notify=notifications.append)
        self.assertEqual(notifications[0]["method"], "notifications/progress")
        self.assertEqual(notifications[0]["params"]["progressToken"], "progress-1")
        self.assertNotIn("isError", response["result"])

    def test_failed_stage_is_not_reported_as_successful_tool(self):
        class FailedClient:
            def call(self, name, arguments, **options):
                return {"status": "error", "next_action": "Inspect privately"}
        server = Server(FailedClient())
        server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        response = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "health"}})
        self.assertTrue(response["result"]["isError"])
        invalid = server.handle({"jsonrpc": "2.0", "id": {}, "method": "ping"})
        self.assertEqual(invalid["error"]["code"], -32600)

    def test_profiles_do_not_share_runtime_or_approvals(self):
        environment = {key: value for key, value in os.environ.items() if key != "LEARNING_TO_SPEC_HOME"}
        with patch.dict(os.environ, environment, clear=True):
            with patch.dict(os.environ, {"COPILOT_HOME": str(ROOT / "test-profile-one")}):
                first = runtime_root()
            with patch.dict(os.environ, {"COPILOT_HOME": str(ROOT / "test-profile-two")}):
                self.assertNotEqual(first, runtime_root())

    def test_real_stdio_has_protocol_only_stdout(self):
        messages = [{"jsonrpc": "2.0", "id": 1, "method": "initialize"},
                    {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                    {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "approve"}}]
        result = subprocess.run([sys.executable, str(ROOT / "scripts/plugin_mcp.py")], input="\n".join(json.dumps(item) for item in messages) + "\n",
                                text=True, capture_output=True, encoding="utf-8", timeout=20, cwd=ROOT.parent)
        self.assertEqual(result.returncode, 0, result.stderr)
        responses = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual([item["id"] for item in responses], [1, 2, 3])
        self.assertEqual(len(responses[1]["result"]["tools"]), 12)
        self.assertTrue(responses[2]["result"]["isError"])

    @unittest.skipUnless(shutil.which("node"), "Node launcher integration")
    def test_node_launcher_works_outside_plugin_directory(self):
        result = subprocess.run(["node", str(ROOT / "scripts/plugin_mcp.cjs")],
                                input='{"jsonrpc":"2.0","id":1,"method":"initialize"}\n',
                                capture_output=True, text=True, encoding="utf-8", timeout=30, cwd=ROOT.parent)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["result"]["serverInfo"]["name"], "learning-to-spec")


class DurableWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.source = self.root / "source/events.jsonl"
        self.source.parent.mkdir()
        self.source.write_text(json.dumps({"type": "user.message", "data": {"content": "Fix retries. Contact PRIVATE_EMAIL@example.invalid"}}) + "\n", encoding="utf-8")
        self.studio = DurableStudio(self.root / "home", self.root / "jobs")

    def tearDown(self):
        self.temporary.cleanup()

    def wait_job(self, identifier):
        for attempt in range(200):
            with self.studio.lock:
                if self.studio.jobs[identifier]["status"] != "running":
                    return self.studio.jobs[identifier]
            time.sleep(0.01)
        self.fail("Worker did not finish")

    def scan(self, delivery="local"):
        result = self.studio.host_action("start_review", {"session": str(self.source), "readers": "both", "delivery": delivery,
            "audience": "root" if delivery == "artifactstore" else "local", "detection": "local", "semantic": False}, "http://127.0.0.1:1234")
        self.assertEqual(self.wait_job(result["job"])["status"], "done")
        return result["job"]

    def approve(self, identifier, action="pseudonymize"):
        review, baseline = load_review(self.studio.jobs[identifier]["directory"] / "review")
        choices = {finding["id"]: {"action": action} for finding in review["findings"]}
        self.studio.action("/api/choices", {"job": identifier, "review_id": review["review_id"], "choices": choices, "confirmed": True})

    def fixture_generation(self, directory, decisions, output, home, **settings):
        apply_review(directory, decisions, output / "reduced")
        (output / "story").mkdir()
        for name in ("human-spec.html", "agent-spec.md", "evidence.md"):
            (output / "story" / name).write_text("# Verified fixture, not a real model test", encoding="utf-8")
        return deliver(output, {"readers": "both", "destination": "local"})

    def test_scan_counts_not_content_and_choices_survive_restart(self):
        identifier = self.scan()
        response = self.studio.snapshot(identifier)
        self.assertEqual(response["findings"], 1)
        self.assertNotIn("PRIVATE_EMAIL", json.dumps(response))
        self.assertNotIn("source_path", json.dumps(response))
        self.assertEqual(response["history"][-1]["status"], "done")
        self.approve(identifier)
        reopened = DurableStudio(self.root / "home", self.root / "jobs")
        self.assertTrue(reopened.snapshot(identifier)["choices_approved"])
        self.assertEqual(reopened.jobs[identifier]["approved_choices"], self.studio.jobs[identifier]["approved_choices"])

    def test_generate_requires_private_approval_and_current_source(self):
        identifier = self.scan()
        with patch("session_spec.studio.generate_private") as generation:
            with self.assertRaisesRegex(ValueError, "save explicit"):
                self.studio.host_action("generate", {"job": identifier}, "")
            generation.assert_not_called()
        self.approve(identifier)
        self.source.write_text(self.source.read_text() + "\n")
        with self.assertRaisesRegex(ValueError, "changed"):
            self.studio.host_action("generate", {"job": identifier}, "")
        self.assertFalse(self.studio.snapshot(identifier)["source_current"])

    def test_real_reduction_delivery_idempotence_and_changed_choices(self):
        identifier = self.scan()
        self.approve(identifier)
        with patch("session_spec.studio.generate_private", side_effect=self.fixture_generation) as generation:
            self.studio.host_action("generate", {"job": identifier}, "")
            self.assertEqual(self.wait_job(identifier)["status"], "done")
            self.studio.host_action("generate", {"job": identifier}, "")
            self.assertEqual(generation.call_count, 1)
        delivery = self.studio.delivered(identifier)
        self.assertEqual({item["name"] for item in delivery["files"]}, {"human-spec.html", "agent-spec.md", "evidence.md"})
        with self.assertRaises(ValueError):
            self.studio.host_action("read_deliverable", {"job": identifier, "name": "../review.json"}, "")
        text = self.studio.host_action("read_deliverable", {"job": identifier, "name": "agent-spec.md", "limit": 10}, "")
        self.assertEqual(len(text["content"]), 10)
        self.assertEqual(text["next_offset"], 10)
        self.approve(identifier, "remove")
        with self.assertRaisesRegex(ValueError, "Choices changed"):
            self.studio.delivered(identifier)

    def test_concurrent_action_cannot_mutate_active_generation(self):
        identifier = self.scan()
        self.approve(identifier)
        started = threading.Event()
        finish = threading.Event()
        def waiting(*arguments, **settings):
            started.set()
            finish.wait(5)
            raise ValueError("Synthetic private failure")
        with patch("session_spec.studio.generate_private", side_effect=waiting):
            self.studio.host_action("generate", {"job": identifier}, "")
            self.assertTrue(started.wait(3))
            original = self.studio.jobs[identifier]["generation"]
            try:
                with self.assertRaisesRegex(ValueError, "running"):
                    self.studio.host_action("generate", {"job": identifier}, "")
                self.assertEqual(self.studio.jobs[identifier]["generation"], original)
            finally:
                finish.set()
                self.wait_job(identifier)
        self.assertNotIn("Synthetic private failure", json.dumps(self.studio.snapshot(identifier)))

    def test_interruption_never_replays_generation_or_upload(self):
        identifier = self.scan()
        job = self.studio.jobs[identifier]
        job.update(status="running", stage="publish")
        self.studio.changed(job)
        with patch("session_spec.studio.publish_package") as publish:
            restored = DurableStudio(self.root / "home", self.root / "jobs")
            self.assertEqual(restored.jobs[identifier]["status"], "error")
            self.assertIn("No operation was replayed", restored.jobs[identifier]["error"])
            publish.assert_not_called()

    def test_existing_publication_blocks_second_write(self):
        identifier = self.scan("artifactstore")
        job = self.studio.jobs[identifier]
        package = job["directory"] / "package"
        package.mkdir()
        (package / "publication.json").write_text('{}')
        job.update(package=package, plan={"plan_id": "example"})
        with patch("session_spec.studio.publish_package") as publish:
            with self.assertRaisesRegex(ValueError, "already attempted"):
                self.studio.action("/api/publish", {"job": identifier, "confirm": "example"})
            publish.assert_not_called()

    def test_preparing_again_reuses_package_and_preserves_plan(self):
        identifier = self.scan("artifactstore")
        job = self.studio.jobs[identifier]
        job.update(generation=job["directory"] / "generation", package=job["directory"] / "package", plan={"plan_id": "previous"})
        manifest = {"package_id": "stable", "files": {}, "findings": []}
        with patch("session_spec.studio.load_package", return_value=manifest) as loading, patch("session_spec.studio.prepare_package") as preparing:
            self.assertEqual(self.studio.action("/api/package", {"job": identifier}), manifest)
            loading.assert_called_once()
            preparing.assert_not_called()
            self.assertEqual(job["plan"]["plan_id"], "previous")

    def test_browser_capability_stays_out_of_tool_response(self):
        identifier = self.scan()
        with patch("session_spec.runtime.open_local") as opening:
            result = self.studio.host_action("open_studio", {"job": identifier}, "http://127.0.0.1:1234")
            self.assertIn(self.studio.token, opening.call_args.args[0])
            self.assertNotIn(self.studio.token, json.dumps(result))
            self.assertNotIn("127.0.0.1", json.dumps(result))

    def test_state_cannot_redirect_private_paths(self):
        identifier = self.scan()
        state_path = self.root / "jobs" / identifier / "job.json"
        saved = json.loads(state_path.read_bytes())
        saved["generation"] = str(self.root / "outside")
        state_path.write_text(json.dumps(saved))
        with self.assertRaisesRegex(ValueError, "inside"):
            DurableStudio(self.root / "home", self.root / "jobs")

    def test_single_writer_lease_released_on_close(self):
        path = self.root / "lease"
        with FileLease(path):
            with self.assertRaisesRegex(ValueError, "owns"):
                with FileLease(path):
                    self.fail("Second owner acquired lease")
        with FileLease(path):
            pass


class RuntimeProcessTests(unittest.TestCase):
    def test_lazy_service_survives_client_and_clean_restart_without_model(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            client = LocalClient(root / "runtime")
            source = root / "source/events.jsonl"
            source.parent.mkdir()
            source.write_text('{"type":"user.message","data":{"content":"Fix retries"}}\n')
            try:
                with patch.dict(os.environ, {"COPILOT_HOME": str(root / "copilot-home")}):
                    self.assertEqual(client.call("health", {})["running"], 0)
                    job = client.call("start_review", {"session": str(source), "readers": "human", "delivery": "local", "audience": "local", "detection": "local", "semantic": False})["job"]
                for attempt in range(100):
                    status = client.request("get_job", {"job": job})
                    if status["status"] != "running":
                        break
                    time.sleep(0.02)
                self.assertEqual(status["status"], "done")
                second = LocalClient(root / "runtime")
                self.assertEqual(second.call("list_jobs", {})["jobs"][0]["job"], job)
                client.request("shutdown", {})
                for attempt in range(100):
                    if not (root / "runtime/endpoint.json").exists():
                        break
                    time.sleep(0.02)
                if client.process:
                    client.process.wait(timeout=10)
                self.assertEqual(second.call("get_job", {"job": job})["status"], "done")
                with self.assertRaises(ValueError):
                    second.call("generate", {"job": job})
            finally:
                try:
                    client.request("shutdown", {})
                except (OSError, ValueError):
                    pass
                for attempt in range(100):
                    if not (root / "runtime/endpoint.json").exists():
                        break
                    time.sleep(0.02)
                for current in (client, locals().get("second")):
                    if current and current.process:
                        current.process.wait(timeout=10)


if __name__ == "__main__":
    unittest.main()
