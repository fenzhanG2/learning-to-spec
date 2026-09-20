import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from session_spec.backend import CopilotBackend, isolated_usage


class UsageReceiptTests(unittest.TestCase):
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

            def communicate(self, *args, **kwargs):
                if kwargs.get("timeout"):
                    raise subprocess.TimeoutExpired("synthetic", 1)
                self.returncode = 1
                return "", ""

            def kill(self):
                self.returncode = 1

        with patch("session_spec.backend.find_copilot", return_value="synthetic"), \
             patch("session_spec.backend.auth_environment", return_value={"COPILOT_GITHUB_TOKEN": "synthetic"}), \
             patch("session_spec.backend.subprocess.Popen", return_value=Process()), \
             patch("session_spec.backend.subprocess.run"), \
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
