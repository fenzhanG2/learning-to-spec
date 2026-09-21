import json
import subprocess
import tempfile
import threading
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from session_spec.backend import (CopilotBackend, PreparationBudget, PreparationCallLimitError,
                                  PreparationTimeoutError, check_preparation_deadline, preparation_budget, run_preparation_process)
from session_spec.model_io import generate_json
from session_spec.fast_story import DraftValidationError
from session_spec.native_bridge import NativeBridge, PIPELINE


class FakeClock:
    now = 0.0

    def __call__(self):
        return self.now


class FakeProcess:
    pid = 12345

    def __init__(self, clock, timeout=False, cleanup_error=False, elapsed=0):
        self.clock = clock
        self.timeout = timeout
        self.cleanup_error = cleanup_error
        self.elapsed = elapsed
        self.returncode = None
        self.timeouts = []

    def communicate(self, timeout):
        self.timeouts.append(timeout)
        if self.cleanup_error or (self.timeout and len(self.timeouts) == 1):
            self.clock.now += timeout
            raise subprocess.TimeoutExpired("synthetic", timeout)
        self.clock.now += self.elapsed
        self.returncode = -9 if self.timeout else 0
        return json.dumps({"type": "assistant.message", "data": {"content": "{}"}}), ""


class NativeBudgetTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.clock = FakeClock()
        self.stack.enter_context(patch("session_spec.backend.time.monotonic", self.clock))
        self.stack.enter_context(patch("session_spec.backend.find_copilot", return_value="synthetic-never-executed"))
        self.stack.enter_context(patch("session_spec.backend.auth_environment", return_value={"COPILOT_GITHUB_TOKEN": "synthetic"}))
        self.popen = self.stack.enter_context(patch("session_spec.backend.subprocess.Popen", side_effect=lambda *args, **kwargs: FakeProcess(self.clock)))
        self.stop = self.stack.enter_context(patch("session_spec.backend.subprocess.run", return_value=subprocess.CompletedProcess([], 0)))
        self.kill_group = self.stack.enter_context(patch("session_spec.backend.os.killpg", create=True))
        self.usage = self.stack.enter_context(patch("session_spec.backend.isolated_usage", return_value=[]))
        self.stack.enter_context(patch("session_spec.backend.probe_copilot_options"))

    def test_default_limits_and_receipt(self):
        with preparation_budget() as budget:
            self.assertEqual((budget.total_seconds, budget.call_seconds, budget.max_calls), (120, 60, 3))
            self.assertEqual(budget.cleanup_reserve, 5)
            self.assertEqual(budget.receipt()["model_calls"], 0)

    def test_invalid_or_over_hard_cap_settings_refused(self):
        for key in ("total_seconds", "call_seconds"):
            for value in (True, False, "60", None, 0, -1, float("nan"), float("inf"), 301 if key == "total_seconds" else 121):
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    PreparationBudget(**{key: value})
        for value in (True, -1, 4, 2.0, "3"):
            with self.subTest(calls=value), self.assertRaises(ValueError):
                PreparationBudget(max_calls=value)

    def test_shared_three_calls_across_separate_backends(self):
        with self.assertRaises(PreparationCallLimitError):
            with preparation_budget() as budget:
                for label in ("draft", "repair", "privacy"):
                    self.assertEqual(CopilotBackend().generate("synthetic", label), {})
                CopilotBackend().generate("synthetic", "must-not-start")
        self.assertEqual(self.popen.call_count, 3)
        self.assertEqual(budget.model_calls, 3)
        self.assertEqual([receipt["shared_call_number"] for receipt in budget.calls[:3]], [1, 2, 3])

    def test_remaining_time_includes_cleanup_reserve(self):
        with preparation_budget() as budget:
            backend = CopilotBackend(timeout=900)
            self.clock.now = 110
            backend.generate("synthetic", "remaining")
            self.assertEqual(backend.calls[0]["timeout_seconds"], 5)
            self.assertEqual(budget.deadline, 120)

    def test_single_call_cap_overrides_legacy_backend_timeout(self):
        with preparation_budget():
            backend = CopilotBackend(timeout=900)
            backend.generate("synthetic", "bounded")
            self.assertEqual(backend.calls[0]["timeout_seconds"], 60)

    def test_smaller_explicit_backend_limit_preserved(self):
        with preparation_budget(call_seconds=75):
            backend = CopilotBackend(timeout=10)
            backend.generate("synthetic", "bounded")
            self.assertEqual(backend.calls[0]["timeout_seconds"], 10)

    def test_expired_deadline_refuses_before_spawn(self):
        with self.assertRaises(PreparationTimeoutError):
            with preparation_budget():
                backend = CopilotBackend()
                self.clock.now = 120
                backend.generate("synthetic", "expired")
        self.popen.assert_not_called()

    def test_cleanup_reserve_cannot_start_another_call(self):
        with self.assertRaises(PreparationTimeoutError):
            with preparation_budget():
                backend = CopilotBackend()
                self.clock.now = 116
                backend.generate("synthetic", "reserved")
        self.popen.assert_not_called()

    def test_scope_checks_local_work_before_success(self):
        with self.assertRaises(PreparationTimeoutError):
            with preparation_budget(total_seconds=1):
                self.clock.now = 2
        check_preparation_deadline()

    def test_nested_scope_cannot_extend_or_reset_budget(self):
        with preparation_budget(total_seconds=12, max_calls=1) as outer:
            with preparation_budget() as inner:
                self.assertIs(inner, outer)
                self.assertEqual(inner.deadline, 12)
                self.assertEqual(inner.max_calls, 1)

    def test_new_scope_does_not_reuse_old_job_budget(self):
        backend = CopilotBackend()
        with preparation_budget(max_calls=1) as first:
            backend.generate("synthetic", "first")
        with preparation_budget(max_calls=1) as second:
            backend.generate("synthetic", "second")
        self.assertIsNot(first, second)
        self.assertEqual((first.model_calls, second.model_calls), (1, 1))

    def test_contexts_are_independent_in_parallel_threads(self):
        barrier = threading.Barrier(2)
        budgets = []

        def worker():
            with preparation_budget(max_calls=1) as budget:
                barrier.wait(timeout=3)
                receipt = {}
                budget.reserve(receipt)
                budget.launched(receipt)
                budgets.append(budget)

        workers = [threading.Thread(target=worker) for _ in range(2)]
        for worker_thread in workers:
            worker_thread.start()
        for worker_thread in workers:
            worker_thread.join(3)
            self.assertFalse(worker_thread.is_alive())
        self.assertEqual(len(budgets), 2)
        self.assertIsNot(budgets[0], budgets[1])
        self.assertEqual([budget.model_calls for budget in budgets], [1, 1])

    def test_backend_captures_budget_when_used_in_worker_thread(self):
        errors = []
        with preparation_budget() as budget:
            backend = CopilotBackend()

            def worker():
                try:
                    backend.generate("synthetic", "thread")
                except BaseException as error:
                    errors.append(error)

            thread = threading.Thread(target=worker)
            thread.start()
            thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(budget.model_calls, 1)

    def test_timeout_terminates_reaps_and_retains_usage(self):
        process = FakeProcess(self.clock, timeout=True)
        self.popen.side_effect = None
        self.popen.return_value = process
        self.usage.return_value = [{"totalApiDurationMs": 10}]
        with self.assertRaises(PreparationTimeoutError) as caught:
            with preparation_budget():
                backend = CopilotBackend()
                backend.generate("synthetic", "timeout")
        self.assertEqual(caught.exception.error_code, "timeout")
        self.assertNotIsInstance(caught.exception, ValueError)
        receipt = backend.calls[0]
        self.assertEqual(receipt["process_cleanup_status"], "confirmed")
        self.assertEqual(receipt["exit_code"], -9)
        self.assertEqual(receipt["session_usage"], [{"totalApiDurationMs": 10}])
        self.assertEqual(receipt["session_usage_source"], "isolated-session-shutdown")
        self.assertEqual(process.timeouts, [60, 5])

    def test_cleanup_failure_has_distinct_marker_and_unknown_usage(self):
        self.popen.side_effect = None
        self.popen.return_value = FakeProcess(self.clock, timeout=True, cleanup_error=True)
        with self.assertRaises(PreparationTimeoutError) as caught:
            with preparation_budget():
                backend = CopilotBackend()
                backend.generate("synthetic", "timeout")
        self.assertEqual(caught.exception.error_code, "cleanup_unconfirmed")
        self.assertIn("Do not retry", str(caught.exception))
        self.assertEqual(backend.calls[0]["session_usage_source"], "unavailable")
        self.assertEqual(backend.calls[0]["process_cleanup_status"], "unconfirmed")

    def test_cleanup_reserve_finishes_at_total_deadline(self):
        self.popen.side_effect = None
        self.popen.return_value = FakeProcess(self.clock, timeout=True, cleanup_error=True)
        with self.assertRaises(PreparationTimeoutError):
            with preparation_budget():
                backend = CopilotBackend()
                self.clock.now = 110
                backend.generate("synthetic", "near-end")
        self.assertEqual(self.clock.now, 120)

    def test_nonzero_tree_termination_is_not_confirmed(self):
        import os
        if os.name != "nt":
            self.skipTest("Windows tree exit code")
        self.popen.side_effect = None
        self.popen.return_value = FakeProcess(self.clock, timeout=True)
        self.stop.return_value = subprocess.CompletedProcess([], 1)
        with self.assertRaises(PreparationTimeoutError) as caught:
            with preparation_budget():
                CopilotBackend().generate("synthetic", "tree")
        self.assertEqual(caught.exception.error_code, "cleanup_unconfirmed")

    def test_timed_out_backend_is_not_retried_as_model_format_error(self):
        self.popen.side_effect = None
        self.popen.return_value = FakeProcess(self.clock, timeout=True)
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(PreparationTimeoutError):
                with preparation_budget():
                    generate_json(CopilotBackend(), "synthetic", "draft", Path(temporary))
            self.assertEqual(list(Path(temporary).iterdir()), [])
        self.assertEqual(self.popen.call_count, 1)

    def test_timeout_is_sticky_even_if_caller_catches_it(self):
        self.popen.side_effect = None
        self.popen.return_value = FakeProcess(self.clock, timeout=True)
        with self.assertRaises(PreparationTimeoutError):
            with preparation_budget():
                try:
                    CopilotBackend().generate("synthetic", "first")
                except PreparationTimeoutError:
                    pass
                CopilotBackend().generate("synthetic", "forbidden-retry")
        self.assertEqual(self.popen.call_count, 1)

    def test_late_process_response_cannot_be_success(self):
        self.popen.side_effect = None
        self.popen.return_value = FakeProcess(self.clock, elapsed=121)
        with self.assertRaises(PreparationTimeoutError):
            with preparation_budget():
                CopilotBackend().generate("synthetic", "late")

    def test_native_fast_dispatch_persists_shared_receipt(self):
        with tempfile.TemporaryDirectory() as temporary:
            bridge = NativeBridge("11111111-1111-4111-8111-111111111111", Path(temporary))
            try:
                source = bridge.capture([{"type": "session.start", "data": {"sessionId": bridge.session_id}},
                                         {"type": "user.message", "data": {"content": "Synthetic CI example"}}], "full")

                def prepare(*args, **settings):
                    self.assertIs(settings["fast"], True)
                    self.assertNotIn("preparation_timeout", settings)
                    CopilotBackend().generate("synthetic", "draft")
                    CopilotBackend().generate("synthetic", "quality")
                    return {"findings": [], "review_id": "synthetic"}

                with patch("session_spec.native_bridge.prepare_abstract_review", side_effect=prepare):
                    result = bridge.call("scan", {"session": source["source"], "privacy_mode": "full", "pipeline": PIPELINE,
                                                  "readers": "both", "delivery": "local", "audience": "local", "detection": "none", "semantic": False})
                    bridge.studio.drain()
                status = bridge.call("status", result)
                self.assertEqual(status["status"], "done")
                receipt = json.loads((bridge.studio.job(result["job"])["directory"] / "preparation-budget.json").read_bytes())
                self.assertEqual((receipt["total_seconds"], receipt["max_calls"], receipt["model_calls"]), (290, 3, 2))
            finally:
                bridge.close()

    def test_native_timeout_only_visible_after_worker_cleanup(self):
        for confirmed in (True, False):
            with self.subTest(confirmed=confirmed), tempfile.TemporaryDirectory() as temporary:
                bridge = NativeBridge("11111111-1111-4111-8111-111111111111", Path(temporary))
                release = threading.Event()
                entered = threading.Event()
                job = {"id": "a" * 32, "directory": bridge.studio.output / ("a" * 32), "stage": "scan", "status": "new"}
                job["directory"].mkdir()
                bridge.studio.jobs[job["id"]] = job

                def operation():
                    entered.set()
                    release.wait(3)
                    raise PreparationTimeoutError(confirmed)

                try:
                    result = bridge.studio.background(job, "scan", operation)
                    self.assertTrue(entered.wait(3))
                    self.assertNotIn("error_code", bridge.call("status", result))
                    release.set()
                    bridge.studio.drain()
                    status = bridge.call("status", result)
                    self.assertEqual(status["status"], "error")
                    self.assertEqual(status["error_code"], "timeout" if confirmed else "cleanup_unconfirmed")
                    if not confirmed:
                        self.assertIn("Do not retry", status["next_action"])
                finally:
                    release.set()
                    bridge.close()

    def test_exhausted_draft_repair_exposes_only_safe_failure_categories(self):
        for issues, code in ((["Agent continuation needs real source refs"], "draft_references_invalid"),
                             (["Invalid JSON"], "draft_structure_invalid")):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as temporary:
                bridge = NativeBridge("11111111-1111-4111-8111-111111111111", Path(temporary))
                job = {"id": "a" * 32, "directory": bridge.studio.output / ("a" * 32), "stage": "scan", "status": "new"}
                job["directory"].mkdir()
                bridge.studio.jobs[job["id"]] = job

                def operation():
                    raise DraftValidationError("PRIVATE_DIAGNOSTIC", issues)

                try:
                    result = bridge.studio.background(job, "scan", operation)
                    bridge.studio.drain()
                    self.assertEqual(bridge.call("status", result)["error_code"], code)
                    progress = bridge.studio.public_progress(job["id"])
                    self.assertEqual(progress["error_code"], code)
                    self.assertNotIn("PRIVATE", json.dumps(progress))
                finally:
                    bridge.close()

    def test_local_renderer_uses_deadline_without_consuming_model_call(self):
        with preparation_budget(total_seconds=10, max_calls=0) as budget:
            result = run_preparation_process(["synthetic"], capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(budget.model_calls, 0)
            self.assertEqual(budget.local_processes[0]["timeout_seconds"], 9)

    def test_final_export_budget_forbids_all_model_calls(self):
        with self.assertRaises(PreparationCallLimitError):
            with preparation_budget(total_seconds=10, max_calls=0):
                CopilotBackend().generate("synthetic", "forbidden-after-approval")
        self.popen.assert_not_called()

    def test_local_renderer_timeout_uses_tree_cleanup(self):
        self.popen.side_effect = None
        self.popen.return_value = FakeProcess(self.clock, timeout=True)
        with self.assertRaises(PreparationTimeoutError) as caught:
            with preparation_budget(total_seconds=10, max_calls=0) as budget:
                run_preparation_process(["synthetic"], capture_output=True, timeout=60)
        self.assertEqual(caught.exception.error_code, "timeout")
        self.assertEqual(budget.local_processes[0]["process_cleanup_status"], "confirmed")

    def test_native_post_selection_budget_excludes_human_wait(self):
        with tempfile.TemporaryDirectory() as temporary:
            bridge = NativeBridge("11111111-1111-4111-8111-111111111111", Path(temporary))
            job = {"id": "a" * 32, "directory": bridge.studio.output / ("a" * 32), "stage": "scan", "status": "done",
                   "pipeline": PIPELINE, "automated_elapsed_seconds": 109}
            job["directory"].mkdir()
            bridge.studio.jobs[job["id"]] = job
            self.clock.now = 10000

            def render():
                result = run_preparation_process(["synthetic"], capture_output=True, timeout=60)
                self.clock.now += 2
                return {"returncode": result.returncode}

            try:
                result = bridge.studio.background(job, "generate", render)
                bridge.studio.drain()
                self.assertEqual(bridge.call("status", result)["status"], "done")
                self.assertEqual(job["automated_elapsed_seconds"], 111)
                receipt = json.loads(next(job["directory"].glob("export-budget-*.json")).read_bytes())
                self.assertEqual((receipt["total_seconds"], receipt["max_calls"]), (10, 0))
            finally:
                bridge.close()

    def test_failed_provider_launch_does_not_claim_a_model_call(self):
        self.popen.side_effect = OSError("synthetic spawn failure")
        with preparation_budget() as budget:
            with self.assertRaises(OSError):
                CopilotBackend().generate("synthetic", "spawn")
            self.assertEqual(budget.model_calls, 0)
            self.assertEqual(budget.reserved_calls, 0)
            self.assertFalse(budget.calls[0]["provider_started"])

    def test_actual_renderer_entry_uses_bounded_helper(self):
        from session_spec.story_pipeline import render_story

        with preparation_budget(total_seconds=10, max_calls=0) as budget:
            with patch("session_spec.story_pipeline.shutil.which", return_value="synthetic-node"), \
                    patch("session_spec.story_pipeline.renderer_entry", return_value=Path("synthetic-render.cjs")):
                render_story(Path("synthetic-support"), Path("synthetic-target"))
            self.assertEqual(budget.local_processes[0]["timeout_seconds"], 9)
            self.assertEqual(self.popen.call_args.args[0], ["synthetic-node", "synthetic-render.cjs", "synthetic-support", "synthetic-target"])

    def test_reasoning_effort_is_optional_and_explicit_in_private_receipt(self):
        backend = CopilotBackend()
        backend.generate("synthetic", "legacy-default")
        self.assertNotIn("--reasoning-effort", self.popen.call_args.args[0])
        self.assertIsNone(backend.calls[0]["requested_reasoning_effort"])
        for effort in ("none", "minimal", "low", "medium", "high", "xhigh", "max"):
            with self.subTest(effort=effort):
                backend = CopilotBackend(reasoning_effort=effort)
                backend.generate("synthetic", "explicit-effort")
                command = self.popen.call_args.args[0]
                self.assertEqual(command[command.index("--reasoning-effort") + 1], effort)
                self.assertEqual(backend.calls[0]["requested_reasoning_effort"], effort)

    def test_invalid_reasoning_effort_rejected_before_provider_start(self):
        for effort in (False, 0, [], {}, "LOW", "ultra", ""):
            with self.subTest(effort=effort), self.assertRaises(ValueError):
                CopilotBackend(reasoning_effort=effort)
        self.popen.assert_not_called()

    def test_provider_effort_rejection_does_not_silently_fallback(self):
        process = FakeProcess(self.clock)

        def rejected(timeout):
            process.returncode = 1
            return "", "Synthetic model rejects requested effort"

        process.communicate = rejected
        self.popen.side_effect = None
        self.popen.return_value = process
        backend = CopilotBackend(reasoning_effort="low")
        with self.assertRaisesRegex(ValueError, "Synthetic model rejects requested effort"):
            backend.generate("synthetic", "effort-rejection")
        self.assertEqual(self.popen.call_count, 1)
        self.assertEqual(backend.calls[0]["exit_code"], 1)
        self.assertEqual(backend.calls[0]["requested_reasoning_effort"], "low")

    def test_auto_tier_is_optional_and_recorded_without_claiming_observed_model(self):
        backend = CopilotBackend()
        backend.generate("synthetic", "default-tier")
        self.assertNotIn("--auto-tier", self.popen.call_args.args[0])
        self.assertIsNone(backend.calls[0]["requested_auto_tier"])
        for tier in ("fast", "efficiency", "balance", "intelligence"):
            with self.subTest(tier=tier):
                backend = CopilotBackend(model="auto", auto_tier=tier)
                backend.generate("synthetic", "explicit-tier")
                command = self.popen.call_args.args[0]
                self.assertEqual(command[command.index("--model") + 1], "auto")
                self.assertEqual(command[command.index("--auto-tier") + 1], tier)
                self.assertNotIn("--reasoning-effort", command)
                self.assertEqual(backend.calls[0]["requested_auto_tier"], tier)
                self.assertIsNone(backend.calls[0]["requested_reasoning_effort"])
                self.assertEqual(backend.calls[0]["requested_model"], "auto")
                self.assertEqual(backend.calls[0]["models"], [])

    def test_auto_reasoning_effort_rejected_before_discovery_auth_or_budget_use(self):
        with patch("session_spec.backend.find_copilot") as discovery, \
                patch("session_spec.backend.auth_environment") as authentication, preparation_budget() as budget:
            for tier in (None, "fast"):
                for effort in ("none", "minimal", "low", "medium", "high", "xhigh", "max"):
                    with self.subTest(tier=tier, effort=effort), self.assertRaisesRegex(ValueError, "auto model does not support reasoning"):
                        CopilotBackend(model="auto", auto_tier=tier, reasoning_effort=effort)
            discovery.assert_not_called()
            authentication.assert_not_called()
            self.popen.assert_not_called()
            self.assertEqual(budget.model_calls, 0)
            self.assertEqual(budget.calls, [])

    def test_invalid_or_explicit_model_auto_tier_refused_before_start(self):
        for tier in (False, 0, [], {}, "FAST", "low", ""):
            with self.subTest(tier=tier), self.assertRaises(ValueError):
                CopilotBackend(model="auto", auto_tier=tier)
        for model in (None, "synthetic-explicit-model"):
            with self.subTest(model=model), self.assertRaises(ValueError):
                CopilotBackend(model=model, auto_tier="fast")
        self.popen.assert_not_called()

    def test_provider_auto_tier_rejection_has_no_fallback(self):
        process = FakeProcess(self.clock)

        def rejected(timeout):
            process.returncode = 1
            return "", "Synthetic provider rejects auto tier"

        process.communicate = rejected
        self.popen.side_effect = None
        self.popen.return_value = process
        backend = CopilotBackend(model="auto", auto_tier="fast")
        with self.assertRaisesRegex(ValueError, "Synthetic provider rejects auto tier"):
            backend.generate("synthetic", "tier-rejection")
        self.assertEqual(self.popen.call_count, 1)
        self.assertEqual(backend.calls[0]["exit_code"], 1)
        self.assertEqual(backend.calls[0]["requested_auto_tier"], "fast")
        self.assertIsNone(backend.calls[0]["effective_auto_tier"])
        self.assertEqual(backend.calls[0]["effective_settings_status"], "dispatched_unconfirmed")


if __name__ == "__main__":
    unittest.main()
