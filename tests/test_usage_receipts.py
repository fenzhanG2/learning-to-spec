import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from session_spec.backend import CopilotBackend, cleanup_guard, isolated_usage


class UsageReceiptTests(unittest.TestCase):
    def test_resource_exit_errors_preserve_the_original_failure(self):
        class Resource:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                raise PermissionError("synthetic retained resource")

        for field in ("worker_cleanup_error", "prompt_cleanup_error"):
            receipt = {}
            with self.assertRaisesRegex(ValueError, "original timeout; cleanup unknown"):
                with cleanup_guard(Resource(), receipt, field, "synthetic-worker"):
                    raise ValueError("original timeout; cleanup unknown")
            self.assertEqual(receipt[field], "PermissionError")
            self.assertEqual(receipt["worker_cleanup_status"], "unconfirmed")
        with self.assertRaisesRegex(ValueError, "worker cleanup was not confirmed"):
            with cleanup_guard(Resource(), {}, "worker_cleanup_error", "synthetic-worker"):
                pass

    @unittest.skipUnless(os.name == "nt", "Windows process-tree termination semantics")
    def test_nonzero_tree_stop_remains_unknown_after_root_drain(self):
        class Process:
            pid = 12345
            returncode = None
            calls = 0

            def communicate(self, *args, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    raise subprocess.TimeoutExpired("synthetic", 1)
                self.returncode = 1
                return "", ""

        with patch("session_spec.backend.find_copilot", return_value="synthetic"), \
             patch("session_spec.backend.auth_environment", return_value={"COPILOT_GITHUB_TOKEN": "synthetic"}), \
             patch("session_spec.backend.subprocess.Popen", return_value=Process()), \
             patch("session_spec.backend.subprocess.run", return_value=subprocess.CompletedProcess([], 1)):
            backend = CopilotBackend(timeout=1)
            with self.assertRaisesRegex(ValueError, "process-tree cleanup was not confirmed"):
                backend.generate("synthetic", "synthetic-tree")
            self.assertEqual(backend.calls[0]["exit_code"], 1)
            self.assertEqual(backend.calls[0]["cleanup_error"], "tree_termination_unconfirmed")

    def test_file_backed_prompt_preserves_utf8_and_newline_bytes(self):
        original_popen = subprocess.Popen
        observed = []
        program = "import sys,json; text=sys.stdin.buffer.read().decode('utf-8'); print(json.dumps({'type':'assistant.message','data':{'content':json.dumps({'text':text})}}))"

        def launch(command, **options):
            self.assertNotEqual(options["stdin"], subprocess.PIPE)
            observed.append(options["stdin"])
            return original_popen([sys.executable, "-B", "-c", program], **options)

        with patch("session_spec.backend.find_copilot", return_value="synthetic"), \
             patch("session_spec.backend.auth_environment", return_value={"COPILOT_GITHUB_TOKEN": "synthetic", "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")}), \
             patch("session_spec.backend.subprocess.Popen", side_effect=launch):
            backend = CopilotBackend(timeout=5)
            text = "Synthetic 中文 α\nsecond\r\nthird\tend"
            self.assertEqual(backend.generate(text, "synthetic-echo"), {"text": text})
        self.assertTrue(observed[0].closed)

    def test_nonreading_child_cannot_block_large_prompt_timeout(self):
        original_popen = subprocess.Popen
        children = []

        def launch(command, **options):
            self.assertNotEqual(options["stdin"], subprocess.PIPE)
            process = original_popen([sys.executable, "-B", "-c", "import time; time.sleep(30)"], **options)
            children.append(process)
            return process

        def stop(command, **options):
            self.assertEqual(options["timeout"], 15)
            children[0].kill()
            return subprocess.CompletedProcess(command, 0, b"", b"")

        with patch("session_spec.backend.find_copilot", return_value="synthetic"), \
             patch("session_spec.backend.auth_environment", return_value={"COPILOT_GITHUB_TOKEN": "synthetic", "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")}), \
             patch("session_spec.backend.subprocess.Popen", side_effect=launch), \
             patch("session_spec.backend.subprocess.run", side_effect=stop):
            backend = CopilotBackend(timeout=1)
            with self.assertRaisesRegex(ValueError, "timed out after"):
                backend.generate("synthetic\n" * 262144, "synthetic-nonreader")
            self.assertTrue(backend.calls[0]["timed_out"])
            self.assertLess(backend.calls[0]["elapsed_seconds"], 8)
            self.assertIsNotNone(children[0].returncode)

    def test_cleanup_timeout_is_bounded_and_not_reported_as_stopped(self):
        class Process:
            pid = 12345
            returncode = None

            def communicate(self, *args, **kwargs):
                self_outer.assertIn(kwargs.get("timeout"), (1, 15))
                raise subprocess.TimeoutExpired("synthetic", kwargs["timeout"])

            def kill(self):
                pass

        self_outer = self
        with patch("session_spec.backend.find_copilot", return_value="synthetic"), \
             patch("session_spec.backend.auth_environment", return_value={"COPILOT_GITHUB_TOKEN": "synthetic"}), \
             patch("session_spec.backend.subprocess.Popen", return_value=Process()), \
             patch("session_spec.backend.subprocess.run", return_value=subprocess.CompletedProcess([], 0)):
            backend = CopilotBackend(timeout=1)
            with self.assertRaisesRegex(ValueError, "cleanup was not confirmed"):
                backend.generate("synthetic", "synthetic-cleanup")
            self.assertIsNone(backend.calls[0]["exit_code"])
            self.assertEqual(backend.calls[0]["cleanup_error"], "TimeoutExpired")

    def test_only_last_shutdown_in_explicit_profile_is_used_without_message_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            events = root / "session-state/synthetic/events.jsonl"
            events.parent.mkdir(parents=True)
            tokens = {"input": {"tokenCount": 10}, "cache_read": {"tokenCount": 20}}
            payloads = [
                {"type": "assistant.message", "data": {"content": "PRIVATE_CANARY", "reasoning": "HIDDEN_CANARY"}},
                {"type": "session.shutdown", "data": {"totalApiDurationMs": 5}},
                {"type": "session.shutdown", "data": {"tokenDetails": {**tokens, "content": "PRIVATE_CANARY"},
                                                        "currentModel": "test-model", "content": "PRIVATE_CANARY"}},
            ]
            events.write_text("\n".join(json.dumps(event) for event in payloads), encoding="utf-8")
            self.assertEqual(isolated_usage(root), [{"tokenDetails": tokens, "currentModel": "test-model"}])
            self.assertEqual(isolated_usage(root / "empty"), [])

    def test_timeout_retains_wall_time_exit_and_own_profile_metrics(self):
        import subprocess

        class Process:
            pid = 12345
            returncode = None
            calls = 0

            def communicate(self, *args, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    raise subprocess.TimeoutExpired("synthetic", 1)
                self.returncode = 1
                return "", ""

            def kill(self):
                self.returncode = 1

        with patch("session_spec.backend.find_copilot", return_value="synthetic"), \
             patch("session_spec.backend.auth_environment", return_value={"COPILOT_GITHUB_TOKEN": "synthetic"}), \
             patch("session_spec.backend.subprocess.Popen", return_value=Process()), \
             patch("session_spec.backend.subprocess.run", return_value=subprocess.CompletedProcess([], 0)), \
             patch("session_spec.backend.isolated_usage", return_value=[{"totalApiDurationMs": 1}]) as usage:
            backend = CopilotBackend(timeout=1)
            with self.assertRaisesRegex(ValueError, "timed out"):
                backend.generate("synthetic prompt", "test-timeout")
            receipt = backend.calls[0]
            self.assertTrue(receipt["timed_out"])
            self.assertIn("elapsed_seconds", receipt)
            self.assertEqual(receipt["exit_code"], 1)
            self.assertEqual(receipt["session_usage"], [{"totalApiDurationMs": 1}])
            self.assertEqual(Path(usage.call_args.args[0]).name, "copilot-home")

    def test_default_profile_is_never_scanned_for_metrics(self):
        class Process:
            returncode = 0

            def communicate(self, *args, **kwargs):
                return json.dumps({"type": "assistant.message", "data": {"content": "{}"}}), ""

        with tempfile.TemporaryDirectory() as temporary, \
             patch("session_spec.backend.find_copilot", return_value="synthetic"), \
             patch("session_spec.backend.auth_environment", return_value={"COPILOT_HOME": temporary}), \
             patch("session_spec.backend.subprocess.Popen", return_value=Process()), \
             patch("session_spec.backend.isolated_usage") as usage:
            backend = CopilotBackend()
            self.assertEqual(backend.generate("synthetic", "test"), {})
            usage.assert_not_called()
            self.assertEqual(backend.calls[0]["session_usage_source"], "unavailable")


if __name__ == "__main__":
    unittest.main()
