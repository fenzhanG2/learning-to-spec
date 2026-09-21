import json
import subprocess
import threading
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from session_spec.backend import (CopilotBackend, CopilotCapabilityError, PreparationTimeoutError,
                                  declared_cli_options, preparation_budget, run_preparation_process)


HELP = "Usage: copilot\n  --auto-tier <tier>  Select routing tier\n  --effort, --reasoning-effort <level>  Select effort\n  --model <model>  Select model\n"


class CapabilityTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.discovery = self.stack.enter_context(patch("session_spec.backend.find_copilot", return_value="exact-synthetic-cli"))
        self.authentication = self.stack.enter_context(patch("session_spec.backend.auth_environment", return_value={
            "COPILOT_GITHUB_TOKEN": "PRIVATE_SYNTHETIC_TOKEN", "COPILOT_HOME": "must-not-use-parent-profile", "COPILOT_AUTO_UPDATE": "false"}))
        self.probes = self.stack.enter_context(patch("session_spec.backend.run_preparation_process", side_effect=self.probe))
        self.provider = self.stack.enter_context(patch("session_spec.backend.subprocess.Popen", side_effect=self.launch))
        self.stack.enter_context(patch("session_spec.backend.subprocess.run", side_effect=AssertionError("Offline capability tests forbid subprocess.run")))
        self.stack.enter_context(patch("session_spec.backend.isolated_usage", return_value=[]))
        self.help = HELP
        self.version = "GitHub Copilot CLI 1.0.87-0.\n"
        self.probe_options = []
        self.provider_options = []

    def probe(self, command, **options):
        self.assertEqual(command[0], "exact-synthetic-cli")
        self.assertIn(command[1], ("--version", "--help"))
        self.assertEqual(options["stdin"], subprocess.DEVNULL)
        self.assertEqual(options["timeout"], 5)
        self.assertEqual(options["env"]["COPILOT_GITHUB_TOKEN"], "PRIVATE_SYNTHETIC_TOKEN")
        self.assertEqual(Path(options["env"]["COPILOT_HOME"]), Path(options["cwd"]) / "copilot-home")
        self.probe_options.append(options)
        return subprocess.CompletedProcess(command, 0, self.version if command[1] == "--version" else self.help, "")

    def launch(self, command, **options):
        self.assertEqual(options["stdin"].read().decode("utf-8"), "PRIVATE_SYNTHETIC_SESSION")
        self.provider_options.append(options)

        class Process:
            pid = 12345
            returncode = 0

            def communicate(self, timeout):
                return json.dumps({"type": "assistant.message", "data": {"content": "{}", "model": "synthetic-observed-model"}}), ""

        return Process()

    def test_supported_tier_probes_exact_worker_environment_before_provider(self):
        with preparation_budget() as budget:
            backend = CopilotBackend(model="auto", auto_tier="fast")
            backend.generate("PRIVATE_SYNTHETIC_SESSION", "draft")
            self.assertEqual(budget.model_calls, 1)
        self.assertEqual(self.probes.call_count, 2)
        self.assertEqual(self.provider.call_count, 1)
        for options in self.probe_options:
            self.assertEqual(options["env"], self.provider_options[0]["env"])
            self.assertEqual(options["cwd"], self.provider_options[0]["cwd"])
        receipt = backend.calls[0]
        self.assertEqual(receipt["cli_capabilities"]["status"], "supported")
        self.assertEqual(receipt["cli_capabilities"]["version"], "1.0.87-0")
        self.assertEqual(receipt["effective_auto_tier"], "fast")
        self.assertEqual(receipt["effective_settings_status"], "cli_accepted")
        self.assertEqual(receipt["requested_model"], "auto")
        self.assertEqual(receipt["models"], ["synthetic-observed-model"])
        self.assertNotIn("PRIVATE_SYNTHETIC", json.dumps(receipt))

    def test_worker_lacking_auto_tier_refuses_without_session_dispatch_or_fallback(self):
        self.version = "GitHub Copilot CLI 1.0.78.\n"
        self.help = "Usage: copilot\n  --model <model>  Select model\n"
        with preparation_budget() as budget:
            backend = CopilotBackend(model="auto", auto_tier="fast")
            with self.assertRaises(CopilotCapabilityError) as caught:
                backend.generate("PRIVATE_SYNTHETIC_SESSION", "draft")
            self.assertEqual(budget.model_calls, 0)
            self.assertEqual(budget.reserved_calls, 0)
        self.provider.assert_not_called()
        receipt = backend.calls[0]
        self.assertIs(caught.exception.receipt, receipt)
        self.assertEqual(receipt["cli_capabilities"]["unsupported_flags"], ["--auto-tier"])
        self.assertEqual(receipt["cli_capabilities"]["version"], "1.0.78")
        self.assertEqual(receipt["requested_auto_tier"], "fast")
        self.assertIsNone(receipt["effective_auto_tier"])
        self.assertIsNone(receipt["effective_model"])
        self.assertEqual(receipt["effective_settings_status"], "not_dispatched")
        self.assertFalse(receipt["provider_started"])
        self.assertNotIsInstance(caught.exception, ValueError)

    def test_prose_mention_is_not_an_advertised_option(self):
        self.help = "Error: --auto-tier is unsupported.\nExample: copilot --auto-tier fast\n"
        backend = CopilotBackend(model="auto", auto_tier="fast")
        with self.assertRaises(CopilotCapabilityError):
            backend.generate("PRIVATE_SYNTHETIC_SESSION", "draft")
        self.provider.assert_not_called()

    def test_option_description_cannot_advertise_another_flag(self):
        for help_text in (
            "  --model <model> Choose a model; --auto-tier is not supported on this build.\n",
            "  --model <model>  Choose a model; --auto-tier is not supported on this build.\n",
            "  --model <model> Choose a model.\n                    --auto-tier is unsupported.\n",
            "  --model <model> Example: copilot --auto-tier fast\n",
        ):
            with self.subTest(help=help_text):
                self.help = help_text
                backend = CopilotBackend(model="auto", auto_tier="fast")
                with self.assertRaises(CopilotCapabilityError):
                    backend.generate("PRIVATE_SYNTHETIC_SESSION", "description-control")
                self.assertEqual(backend.calls[0]["cli_capabilities"]["supported_requested_flags"], [])
                self.assertEqual(backend.calls[0]["effective_settings_status"], "not_dispatched")
        self.provider.assert_not_called()

    def test_real_declaration_aliases_advertise_the_option(self):
        self.help = "Usage: copilot\n  -t, --routing-tier, --auto-tier <tier>  Select tier; --reasoning-effort is unsupported.\n"
        backend = CopilotBackend(model="auto", auto_tier="fast")
        backend.generate("PRIVATE_SYNTHETIC_SESSION", "genuine-alias")
        self.assertEqual(backend.calls[0]["cli_capabilities"]["supported_requested_flags"], ["--auto-tier"])
        self.assertEqual(self.provider.call_count, 1)

    def test_real_mixed_help_columns_keep_long_only_options(self):
        self.help = ("Options:\n"
                     "      --model <model>  Select model\n"
                     "      --reasoning-effort <level>  Set reasoning effort\n"
                     "      --auto-tier <preference>  Select routing preference\n"
                     "  -r, --resume [<value>]  Resume a session\n")
        self.assertEqual(declared_cli_options(self.help), {"--model", "--reasoning-effort", "--auto-tier", "--resume"})
        for settings in ({"model": "auto", "auto_tier": "fast"}, {"model": "synthetic-specific", "reasoning_effort": "low"}):
            with self.subTest(settings=settings):
                backend = CopilotBackend(**settings)
                backend.generate("PRIVATE_SYNTHETIC_SESSION", "mixed-column-help")
                self.assertEqual(backend.calls[0]["cli_capabilities"]["status"], "supported")
        self.assertEqual(self.provider.call_count, 2)

    def test_mixed_help_columns_still_exclude_inline_and_continuation_prose(self):
        self.help = ("Options:\n"
                     "      --model <model>  Choose a model; --auto-tier is unsupported.\n"
                     "                        --reasoning-effort is unsupported here too.\n"
                     "  -r, --resume [<value>]  Resume; do not pass --auto-tier.\n")
        self.assertEqual(declared_cli_options(self.help), {"--model", "--resume"})
        backend = CopilotBackend(model="auto", auto_tier="fast")
        with self.assertRaises(CopilotCapabilityError):
            backend.generate("PRIVATE_SYNTHETIC_SESSION", "mixed-column-negative")
        self.provider.assert_not_called()

    def assert_model_selection_without_optional_flags(self, command, model):
        self.assertEqual(command[0], "exact-synthetic-cli")
        self.assertEqual([command[index + 1] for index, value in enumerate(command) if value == "--model"],
                         [] if model is None else [model])
        self.assertFalse(any(value.startswith("--model=") for value in command))
        for flag in ("--auto-tier", "--reasoning-effort", "--effort"):
            self.assertFalse(any(value == flag or value.startswith(flag + "=") for value in command), flag)

    def test_default_omits_model_tier_and_effort_without_capability_probe(self):
        with preparation_budget() as budget:
            backend = CopilotBackend()
            self.assertEqual(backend.generate("PRIVATE_SYNTHETIC_SESSION", "default"), {})
            self.assertEqual(budget.model_calls, 1)
        self.provider.assert_called_once()
        self.assert_model_selection_without_optional_flags(self.provider.call_args.args[0], None)
        self.probes.assert_not_called()
        self.assertIsNone(backend.model)
        receipt = backend.calls[0]
        self.assertEqual(receipt["requested_model"], "copilot-default")
        self.assertEqual(receipt["effective_model"], "copilot-default")
        for key in ("requested_auto_tier", "requested_reasoning_effort", "effective_auto_tier", "effective_reasoning_effort"):
            self.assertIsNone(receipt[key])

    def test_explicit_auto_and_named_selection_are_preserved_without_optional_flags(self):
        for model in ("auto", "synthetic-specific"):
            with self.subTest(model=model), preparation_budget() as budget:
                previous_launches = self.provider.call_count
                backend = CopilotBackend(model=model)
                self.assertEqual(backend.generate("PRIVATE_SYNTHETIC_SESSION", "explicit-model"), {})
                self.assertEqual(budget.model_calls, 1)
                self.assertEqual(self.provider.call_count, previous_launches + 1)
                self.assert_model_selection_without_optional_flags(self.provider.call_args.args[0], model)
                self.assertEqual(backend.model, model)
                self.assertEqual(len(backend.calls), 1)
                receipt = backend.calls[0]
                self.assertEqual(receipt["requested_model"], model)
                self.assertEqual(receipt["effective_model"], model)
                self.assertEqual(receipt["models"], ["synthetic-observed-model"])
                for key in ("requested_auto_tier", "requested_reasoning_effort", "effective_auto_tier", "effective_reasoning_effort"):
                    self.assertIsNone(receipt[key])
        self.probes.assert_not_called()

    def test_unavailable_chosen_model_fails_once_without_substitution_or_other_model_launch(self):
        class UnavailableProcess:
            pid = 12345
            returncode = 1

            def communicate(self, timeout):
                return "", "The chosen model synthetic-unavailable is not available."

        def unavailable(command, **options):
            self.assertEqual(self.provider.call_count, 1, "No fallback provider launch")
            self.launch(command, **options)
            return UnavailableProcess()

        self.provider.side_effect = unavailable
        with preparation_budget() as budget:
            backend = CopilotBackend(model="synthetic-unavailable")
            with self.assertRaisesRegex(ValueError, "Copilot failed: .*synthetic-unavailable is not available"):
                backend.generate("PRIVATE_SYNTHETIC_SESSION", "unavailable-model")
            self.assertEqual(budget.model_calls, 1)
            self.assertEqual(budget.reserved_calls, 0)
        self.provider.assert_called_once()
        self.probes.assert_not_called()
        self.discovery.assert_called_once()
        self.authentication.assert_called_once()
        self.assert_model_selection_without_optional_flags(self.provider.call_args.args[0], "synthetic-unavailable")
        self.assertEqual(backend.model, "synthetic-unavailable")
        self.assertEqual(len(backend.calls), 1)
        receipt = backend.calls[0]
        self.assertEqual(receipt["requested_model"], "synthetic-unavailable")
        self.assertEqual(receipt["exit_code"], 1)
        self.assertTrue(receipt["provider_started"])
        self.assertEqual(receipt["effective_settings_status"], "dispatched_unconfirmed")
        for key in ("effective_model", "effective_auto_tier", "effective_reasoning_effort"):
            self.assertIsNone(receipt[key])
        self.assertNotIn("models", receipt)

    def test_reasoning_alias_is_detected_and_missing_flag_is_refused(self):
        backend = CopilotBackend(model="synthetic-specific", reasoning_effort="low")
        backend.generate("PRIVATE_SYNTHETIC_SESSION", "specific")
        self.assertEqual(backend.calls[0]["effective_reasoning_effort"], "low")
        self.help = "Usage: copilot\n  --model <model>  Select model\n"
        with self.assertRaises(CopilotCapabilityError):
            backend.generate("PRIVATE_SYNTHETIC_SESSION", "changed-runtime")
        self.assertEqual(self.provider.call_count, 1)

    def test_auto_reasoning_conflict_is_rejected_before_discovery_auth_or_probes(self):
        with self.assertRaises(ValueError):
            CopilotBackend(model="auto", auto_tier="fast", reasoning_effort="low")
        self.discovery.assert_not_called()
        self.authentication.assert_not_called()
        self.probes.assert_not_called()
        self.provider.assert_not_called()

    def test_help_nonzero_or_missing_output_is_fail_closed(self):
        for result in (subprocess.CompletedProcess([], 1, "", "PRIVATE_SYNTHETIC_ERROR"),
                       subprocess.CompletedProcess([], 0, "", "")):
            with self.subTest(result=result.returncode):
                self.probes.side_effect = None
                self.probes.return_value = result
                backend = CopilotBackend(model="auto", auto_tier="fast")
                with self.assertRaises(CopilotCapabilityError):
                    backend.generate("PRIVATE_SYNTHETIC_SESSION", "probe-failed")
                self.assertFalse(backend.calls[0]["provider_started"])
                self.assertNotIn("PRIVATE_SYNTHETIC", json.dumps(backend.calls[0]))
        self.provider.assert_not_called()

    def test_probe_os_and_timeout_failures_do_not_dispatch(self):
        for failure in (OSError("PRIVATE_SYNTHETIC_PATH"), subprocess.TimeoutExpired("synthetic", 5)):
            with self.subTest(failure=type(failure).__name__):
                self.probes.side_effect = failure
                backend = CopilotBackend(model="auto", auto_tier="fast")
                with self.assertRaises(CopilotCapabilityError):
                    backend.generate("PRIVATE_SYNTHETIC_SESSION", "probe-failed")
                self.assertEqual(backend.calls[0]["cli_capabilities"]["status"], "failed")
        self.provider.assert_not_called()

    def test_shared_deadline_cleanup_marker_survives_probe_failure(self):
        self.probes.side_effect = PreparationTimeoutError(False)
        backend = CopilotBackend(model="auto", auto_tier="fast")
        with self.assertRaises(PreparationTimeoutError) as caught:
            backend.generate("PRIVATE_SYNTHETIC_SESSION", "probe-timeout")
        self.assertEqual(caught.exception.error_code, "cleanup_unconfirmed")
        self.provider.assert_not_called()

    def test_capabilities_are_rechecked_in_each_new_worker_profile(self):
        backend = CopilotBackend(model="auto", auto_tier="fast")
        backend.generate("PRIVATE_SYNTHETIC_SESSION", "first")
        backend.generate("PRIVATE_SYNTHETIC_SESSION", "second")
        self.assertEqual(self.probes.call_count, 4)
        self.assertNotEqual(self.probe_options[0]["cwd"], self.probe_options[2]["cwd"])

    def captured_budget_at_cleanup_reserve(self, threaded):
        now = [0.0]
        self.probes.side_effect = run_preparation_process
        errors = []
        with patch("session_spec.backend.time.monotonic", side_effect=lambda: now[0]), \
                patch("session_spec.backend.subprocess.run", side_effect=AssertionError("Unbounded probe forbidden")):
            with preparation_budget(total_seconds=10) as budget:
                backend = CopilotBackend(model="auto", auto_tier="fast")
            now[0] = 9

            def generate():
                try:
                    backend.generate("PRIVATE_SYNTHETIC_SESSION", "captured-reserve")
                except BaseException as error:
                    errors.append(error)

            if threaded:
                worker = threading.Thread(target=generate)
                worker.start()
                worker.join(3)
                self.assertFalse(worker.is_alive())
            else:
                generate()
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], PreparationTimeoutError)
            self.assertEqual(budget.deadline, 10)
            self.assertEqual(now[0], 9)
            self.assertEqual(budget.model_calls, 0)
            self.assertEqual(len(budget.local_processes), 1)
            self.assertNotIn("process_id", budget.local_processes[0])
            self.assertEqual(backend.calls[0]["cli_capabilities"]["status"], "failed")
            self.provider.assert_not_called()

    def test_captured_budget_after_context_exit_refuses_probe_at_cleanup_reserve(self):
        self.captured_budget_at_cleanup_reserve(False)

    def test_captured_budget_on_worker_thread_refuses_probe_at_cleanup_reserve(self):
        self.captured_budget_at_cleanup_reserve(True)

    def test_worker_probe_timeout_uses_captured_deadline_and_cleanup_receipt(self):
        now = [0.0]
        timeouts = []
        errors = []
        self.probes.side_effect = run_preparation_process

        class ProbeProcess:
            pid = 12345
            returncode = None

            def communicate(self, timeout):
                timeouts.append(timeout)
                if len(timeouts) == 1:
                    now[0] += timeout
                    raise subprocess.TimeoutExpired("synthetic-help", timeout)
                self.returncode = -9
                return "", ""

        def launch(command, **options):
            self.assertEqual(command, ["exact-synthetic-cli", "--version"])
            self.assertEqual(options["stdin"], subprocess.DEVNULL)
            self.assertNotIn("budget", options)
            return ProbeProcess()

        def stop(command, **options):
            self.assertEqual(command[0], "taskkill")
            self.assertEqual(options["timeout"], 1)
            return subprocess.CompletedProcess(command, 0)

        self.provider.side_effect = launch
        with patch("session_spec.backend.time.monotonic", side_effect=lambda: now[0]), \
                patch("session_spec.backend.subprocess.run", side_effect=stop), \
                patch("session_spec.backend.os.killpg", create=True):
            with preparation_budget(total_seconds=10) as budget:
                backend = CopilotBackend(model="auto", auto_tier="fast")
            now[0] = 8

            def generate():
                try:
                    backend.generate("PRIVATE_SYNTHETIC_SESSION", "captured-timeout")
                except BaseException as error:
                    errors.append(error)

            worker = threading.Thread(target=generate)
            worker.start()
            worker.join(3)
            self.assertFalse(worker.is_alive())
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], PreparationTimeoutError)
            self.assertEqual(errors[0].error_code, "timeout")
            self.assertEqual(timeouts, [1, 1])
            self.assertEqual(budget.deadline, 10)
            self.assertEqual(budget.model_calls, 0)
            self.assertEqual(self.provider.call_count, 1)
            self.assertEqual(len(budget.local_processes), 1)
            self.assertEqual(budget.local_processes[0]["process_cleanup_status"], "confirmed")
            self.assertEqual(budget.local_processes[0]["exit_code"], -9)
            self.assertFalse(backend.calls[0]["provider_started"])


if __name__ == "__main__":
    unittest.main()
