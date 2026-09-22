import tempfile
import unittest
from pathlib import Path

from native_abstract_fixture import mocked_abstraction
from offline_provider import guard_offline_test
from session_spec.native_bridge import PIPELINE, NativeBridge


SESSION = "11111111-1111-4111-8111-111111111111"


class NativeModelSelectionTests(unittest.TestCase):
    def setUp(self):
        guard_offline_test(self)
        self.temporary = tempfile.TemporaryDirectory()
        self.bridge = NativeBridge(SESSION, Path(self.temporary.name))
        captured = self.bridge.capture([
            {"type": "session.start", "data": {"sessionId": SESSION}},
            {"type": "user.message", "data": {"content": "Synthetic CI task"}},
        ], "full")
        self.arguments = {"session": captured["source"], "readers": "both", "delivery": "local", "audience": "local",
                          "privacy_mode": "full", "detection": "none", "semantic": False, "pipeline": PIPELINE}

    def tearDown(self):
        self.bridge.close()
        self.temporary.cleanup()

    def prepare(self, **extra):
        with mocked_abstraction() as drafting:
            result = self.bridge.call("scan", {**self.arguments, **extra})
            self.bridge.studio.drain()
        self.assertEqual(self.bridge.call("status", result)["status"], "done")
        self.assertNotIn("model_selection", self.bridge.call("status", result))
        return drafting.call_args.kwargs.get("model"), self.bridge.studio.job(result["job"])["model_selection"]

    def test_each_job_uses_current_host_selection_without_changing_global_settings(self):
        for model in ("synthetic-selected", "auto", None):
            with self.subTest(model=model):
                chosen, receipt = self.prepare(**({"host_model": model} if model else {}))
                self.assertEqual(chosen, model)
                self.assertEqual(receipt, {"requested": model or "copilot-default", "source": "host" if model else "cli-default"})
                self.assertIsNone(self.bridge.studio.settings.get("model"))

    def test_explicit_configured_override_is_preserved(self):
        self.bridge.studio.settings["model"] = "synthetic-configured"
        chosen, receipt = self.prepare(host_model="synthetic-host")
        self.assertEqual(chosen, "synthetic-configured")
        self.assertEqual(receipt["source"], "configured")

    def test_invalid_host_model_refuses_before_creating_a_job(self):
        for model in ("", "--model", "two words", "name\n", "a" * 161, 1, True, {}):
            with self.subTest(model=model), self.assertRaisesRegex(ValueError, "Invalid active-session model"):
                self.bridge.call("scan", {**self.arguments, "host_model": model})
        self.assertEqual(self.bridge.studio.jobs, {})


if __name__ == "__main__":
    unittest.main()
