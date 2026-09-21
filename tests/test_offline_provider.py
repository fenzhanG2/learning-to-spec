import threading
import tempfile
import unittest
from unittest.mock import patch

from native_abstract_fixture import mocked_abstraction
from offline_provider import forbid_live_provider, guard_offline_test
from session_spec.backend import CopilotBackend


class OfflineProviderTests(unittest.TestCase):
    def test_imported_constructor_is_blocked_before_auth_or_spawn(self):
        with patch("session_spec.backend.subprocess.Popen", side_effect=AssertionError("No spawn")) as spawn:
            with self.assertRaisesRegex(AssertionError, "Offline tests forbid real provider entry: CopilotBackend.__init__"):
                with forbid_live_provider():
                    CopilotBackend()
            spawn.assert_not_called()

    def test_existing_backend_generation_is_also_blocked(self):
        backend = CopilotBackend.__new__(CopilotBackend)
        with self.assertRaisesRegex(AssertionError, "Offline tests forbid real provider entry: CopilotBackend.generate"):
            with forbid_live_provider():
                backend.generate("synthetic", "must-not-run")

    def test_worker_cannot_hide_attempt_even_if_its_exception_is_caught(self):
        failures = []

        def worker():
            try:
                CopilotBackend()
            except AssertionError as error:
                failures.append(str(error))

        with self.assertRaisesRegex(AssertionError, "Called 1 times"):
            with forbid_live_provider():
                thread = threading.Thread(target=worker)
                thread.start()
                thread.join(3)
                self.assertFalse(thread.is_alive())
        self.assertEqual(len(failures), 1)
        self.assertIn("Offline tests forbid", failures[0])

    def test_guard_survives_teardown_and_restores_after_cleanup(self):
        original = CopilotBackend.__init__
        case = unittest.TestCase()
        guard_offline_test(case)
        guarded = CopilotBackend.__init__
        self.assertIsNot(guarded, original)
        self.assertTrue(case.doCleanups())
        self.assertIs(CopilotBackend.__init__, original)

    def test_infrastructure_fixture_explicitly_redirects_native_fast_dispatch(self):
        from session_spec import native_bridge

        with patch("session_spec.abstract_privacy.prepare_abstract_review", return_value={"fixture": "not-fast-e2e"}) as prepare:
            with mocked_abstraction():
                result = native_bridge.prepare_abstract_review("synthetic-source", fast=True, privacy_mode="full")
            self.assertEqual(result, {"fixture": "not-fast-e2e"})
            prepare.assert_called_once_with("synthetic-source", fast=False, privacy_mode="full")

    def test_obsolete_run_story_only_mock_attempts_provider_and_fails_closed(self):
        from session_spec.native_bridge import PIPELINE, NativeBridge

        with tempfile.TemporaryDirectory() as temporary:
            bridge = NativeBridge("11111111-1111-4111-8111-111111111111", temporary)
            try:
                captured = bridge.capture([
                    {"type": "session.start", "data": {"sessionId": bridge.session_id}},
                    {"type": "user.message", "data": {"content": "Synthetic obsolete fixture diagnostic"}},
                ], "full")
                with self.assertRaisesRegex(AssertionError, "Called 1 times"):
                    with forbid_live_provider(), patch("session_spec.story_pipeline.run_story") as obsolete:
                        job = bridge.call("scan", {"session": captured["source"], "readers": "both", "delivery": "local",
                                                   "audience": "local", "privacy_mode": "full", "detection": "none",
                                                   "semantic": False, "pipeline": PIPELINE})
                        bridge.studio.drain()
                        self.assertEqual(bridge.call("status", job)["status"], "error")
                        obsolete.assert_not_called()
            finally:
                bridge.close()


if __name__ == "__main__":
    unittest.main()
