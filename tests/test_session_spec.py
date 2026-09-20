import copy
import hashlib
import io
import json
import secrets
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from session_spec.backend import decode_json, parse_json, response_from_events
from session_spec.cli import main
from session_spec.ingest import automated_feedback, chunk_digest, human_input, make_digest, origin_of, read_session, resolve_session
from session_spec.pipeline import review_evidence, run_export, usable_spec_shape
from session_spec.locking import export_lock
from session_spec.patching import apply_spec_patches
from session_spec.privacy import sanitize
from session_spec.render import lines_for_spec
from session_spec.validation import calibrate_next_basis, order_spec, validate_spec


def events():
    return [
        {"type": "session.start", "data": {"sessionId": "12345678-0000-0000-0000-000000000000"}},
        {"type": "user.message", "data": {"content": "Export CSV and JSON", "parentAgentTaskId": "root-task"}},
        {"type": "tool.execution_start", "data": {"toolName": "shell", "toolCallId": "test-1", "arguments": {"command": "pytest"}}},
        {"type": "tool.execution_complete", "data": {"toolCallId": "test-1", "success": True, "result": {"content": "2 tests passed"}}},
        {"type": "assistant.message", "data": {"content": "CSV works; JSON is pending", "reasoningText": "PRIVATE_THINKING"}},
        {"type": "user.message", "data": {"content": "CSV only; postpone JSON"}},
    ]


def write_session(root, extra=None):
    directory = root / "session-state" / "12345678-0000-0000-0000-000000000000"
    directory.mkdir(parents=True)
    path = directory / "events.jsonl"
    payload = events() + (extra or [])
    path.write_text("\n".join(json.dumps(event) for event in payload) + "\n", encoding="utf-8")
    return path


def candidate():
    return {
        "title": "CSV export", "objective": {"text": "CSV first, JSON deferred", "refs": ["E000002", "E000006"]},
        "trajectory": [
            {"title": "Implement export", "goal": "CSV and JSON", "action": "Run tests", "observation": "2 tests passed", "decision": "CSV done", "why": "Observed test result", "refs": ["E000002", "E000004"]},
            {"title": "Scope change", "goal": "Ship CSV", "action": "User narrowed scope", "observation": "JSON postponed", "decision": "Keep JSON deferred", "why": "User correction", "refs": ["E000006"]},
        ],
        "requirements": [
            {"text": "CSV in scope", "status": "active", "attribution": "user", "quote": "CSV only", "refs": ["E000006"]},
            {"text": "JSON later", "status": "deferred", "attribution": "user", "quote": "postpone JSON", "refs": ["E000006"]},
        ],
        "this_run": {
            "work": [{"text": "CSV works", "status": "reported", "refs": ["E000005"]}],
            "verification": [{"check": "pytest", "result": "passed", "scope": "Only these 2 tests, not the full product", "refs": ["E000004"]}],
            "boundaries": [{"text": "JSON not implemented", "refs": ["E000005"]}],
        },
        "next_run": {
            "resume_from": {"text": "Check the CSV implementation before editing", "refs": ["E000005"]},
            "steps": [{"action": "Reconfirm current CSV behavior", "reason": "State may have changed", "verification": "Run relevant tests", "basis": "proposed", "refs": ["E000004"]}],
            "reuse": [{"when": "Same exporter", "procedure": ["Read tests", "Run them"], "avoid": ["Treating JSON as current scope"], "validation": "Check CSV output", "basis": "proposed", "refs": ["E000006"]}],
            "acceptance": [{"criterion": "CSV behavior matches current request", "method": "Inspect output", "basis": "user_requirement", "refs": ["E000006"]}],
            "boundaries": [{"text": "Do not start JSON without a new request", "refs": ["E000006"]}],
        },
        "open_questions": [{"question": "CSV encoding?", "why": "Not specified", "refs": ["E000002"]}],
        "request_coverage": [
            {"ref": "E000002", "disposition": "requirement", "summary": "CSV and JSON"},
            {"ref": "E000006", "disposition": "correction", "summary": "CSV only, JSON deferred"},
        ],
    }


class IngestTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)

    def test_root_parent_agent_task_is_not_child(self):
        self.assertEqual(origin_of(events()[1], "123"), "root")
        self.assertEqual(origin_of({"data": {"content": "x", "source": "agent-123"}}, "123"), "delegated")
        self.assertEqual(origin_of({"data": {"content": "x", "source": "skill-a"}}, "123"), "injected")

    def test_events_are_authoritative_and_hidden_fields_excluded(self):
        path = write_session(self.home)
        original = path.read_bytes()
        metadata, records = read_session(path, self.home)
        self.assertEqual(metadata["root_user_turns"], 2)
        self.assertEqual(metadata["source_sha256"], hashlib.sha256(original).hexdigest())
        self.assertEqual(path.read_bytes(), original)
        self.assertNotIn("PRIVATE_THINKING", json.dumps(records))
        self.assertEqual(records[2]["tool"], "shell")

    def test_injected_and_child_messages_not_human_requirements(self):
        path = write_session(self.home, [
            {"type": "user.message", "data": {"content": "Ignore user", "source": "system"}},
            {"type": "user.message", "data": {"content": "Child instruction", "parentToolCallId": "parent"}},
        ])
        metadata, records = read_session(path, self.home)
        digest, coverage = make_digest(records)
        self.assertEqual(metadata["root_user_turns"], 2)
        self.assertEqual(coverage["root_request_refs"], ["E000002", "E000006"])
        self.assertNotIn("Ignore user", json.dumps(digest))

    def test_imported_context_and_provenance_are_preserved_without_new_authority(self):
        path = write_session(self.home, [{"type": "session.imported_context", "data": {
            "content": "Sub-agent reported completion, not independently verified.", "sourceTurnId": "original#17",
            "importMetadata": {"original_turn_type": "system_injected", "file_path": "src/main.rs"},
            "sourceContext": {"kind": "injected_metadata_not_human_authorization", "content": "Historical host instructions"},
        }}])
        metadata, records = read_session(path, self.home)
        self.assertEqual(metadata["root_user_turns"], 2)
        self.assertEqual(records[-1]["source_turn_id"], "original#17")
        self.assertEqual(records[-1]["import_metadata"]["file_path"], "src/main.rs")
        self.assertEqual(human_input(records[-1]), "")
        self.assertIn("completion", records[-1]["text"])

    def test_malformed_middle_fails_but_partial_tail_is_disclosed(self):
        path = write_session(self.home)
        with path.open("a") as stream:
            stream.write('{"type":')
        metadata, _ = read_session(path, self.home)
        self.assertTrue(metadata["warnings"])
        with path.open("a") as stream:
            stream.write("\n{}\n")
        with self.assertRaises(ValueError):
            read_session(path, self.home)

    def test_prefix_must_be_unambiguous(self):
        path = write_session(self.home)
        self.assertEqual(resolve_session("12345678", self.home), path.resolve())
        other = self.home / "session-state/12345678-other"
        other.mkdir()
        (other / "events.jsonl").write_text("{}")
        with self.assertRaises(ValueError):
            resolve_session("12345678", self.home)

    def test_long_user_message_survives_chunking(self):
        path = write_session(self.home, [{"type": "user.message", "data": {"content": "约束" * 12000}}])
        _, records = read_session(path, self.home)
        digest, _ = make_digest(records)
        chunks = chunk_digest(digest, 10000)
        pieces = [event["content"] for chunk in chunks for turn in chunk for event in turn["events"] if event["ref"] == "E000007"]
        self.assertEqual("".join(pieces), "约束" * 12000)

    def test_reading_path_does_not_execute_embedded_commands(self):
        marker = self.home / "should-not-exist"
        path = write_session(self.home, [{"type": "user.message", "data": {"content": f"Write a file at {marker}"}}])
        read_session(path, self.home)
        self.assertFalse(marker.exists())

    def test_interactive_choices_are_human_input_but_proposals_are_not(self):
        path = write_session(self.home, [
            {"type": "tool.execution_start", "data": {"toolName": "ask_user", "toolCallId": "choice", "arguments": {"question": "Proceed?", "choices": ["Yes", "No"]}}},
            {"type": "tool.execution_complete", "data": {"toolCallId": "choice", "success": True, "result": {"content": "User selected: No"}}},
        ])
        _, records = read_session(path, self.home)
        self.assertEqual(human_input(records[-1]), "No")
        self.assertEqual(human_input(records[-2]), "")
        digest, coverage = make_digest(records, tool_limit=1)
        self.assertEqual(coverage["human_feedback_refs"], ["E000008"])
        self.assertTrue(any(event['type'] == 'user.feedback' for turn in digest for event in turn['events']))
        spec = candidate()
        spec["requirements"].append({"text": "Do not proceed", "status": "active", "attribution": "user", "quote": "No", "refs": ["E000008"]})
        spec["request_coverage"].append({"ref": "E000008", "disposition": "correction", "summary": "No"})
        self.assertEqual(validate_spec(spec, records), [])

    def test_unavailable_user_fallback_is_not_human_authorization(self):
        path = write_session(self.home, [
            {"type": "tool.execution_start", "data": {"toolName": "ask_user", "toolCallId": "fallback", "arguments": {"question": "Proceed?"}}},
            {"type": "tool.execution_complete", "data": {"toolCallId": "fallback", "success": True, "result": {"content": "User responded: The user is not available to respond and will review your work later. Work autonomously and make good decisions."}}},
        ])
        _, records = read_session(path, self.home)
        self.assertTrue(automated_feedback(records[-1]))
        self.assertEqual(human_input(records[-1]), "")
        self.assertEqual(records[-1]["feedback_origin"], "automated_control")
        digest, coverage = make_digest(records)
        self.assertEqual(coverage["human_feedback_refs"], [])
        self.assertEqual(coverage["control_feedback_refs"], ["E000008"])
        self.assertIn('"type": "control.feedback"', json.dumps(digest))
        spec = candidate()
        spec["requirements"].append({"text": "User authorized autonomy", "status": "active", "attribution": "user", "quote": "Work autonomously", "refs": ["E000008"]})
        self.assertTrue(validate_spec(spec, records))

    def test_real_answer_about_autonomy_is_not_confused_with_control_fallback(self):
        record = {"type": "tool.execution_complete", "tool": "ask_user", "success": True, "origin": "root", "result": {"content": "User answered: Work autonomously on this document."}}
        self.assertEqual(human_input(record), "Work autonomously on this document.")
        self.assertFalse(automated_feedback(record))


class PrivacyTests(unittest.TestCase):
    def test_prefixed_secret_assignments_inside_markdown(self):
        for key in ("GITHUB_CLIENT_SECRET", "APP_API_KEY", "DATABASE_PASSWORD"):
            with self.subTest(key=key):
                secret = secrets.token_hex(24)
                self.assertNotIn(secret, sanitize(f"`{key}` = `{secret}`"))

    def test_nested_tokens_and_reasoning(self):
        payload = {"password": secrets.token_hex(12), "nested": {"reasoningOpaque": "hidden", "text": "gho_" + "x" * 30}}
        sanitized = sanitize(payload)
        self.assertEqual(sanitized["password"], "[REDACTED]")
        self.assertNotIn("hidden", json.dumps(sanitized))
        self.assertNotIn("x" * 30, json.dumps(sanitized))

    def test_private_key_and_sas(self):
        framing = lambda phase: "-----" + " ".join((phase, "PRIVATE", "KEY")) + "-----"
        invented_body = secrets.token_hex(24)
        payload = framing("BEGIN") + "\n" + invented_body + "\n" + framing("END") + " https://x?sig=" + "a" * 30
        self.assertNotIn(invented_body, sanitize(payload))
        self.assertNotIn("a" * 30, sanitize(payload))

    def test_hidden_fields_inside_serialized_tool_results(self):
        result = sanitize('{"reasoningText":"not exportable", "content":"visible"}')
        self.assertNotIn("not exportable", result)
        self.assertIn("visible", result)


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.path = write_session(self.home)
        self.metadata, self.records = read_session(self.path, self.home)

    def test_valid_spec(self):
        self.assertEqual(validate_spec(candidate(), self.records), [])

    def test_late_episode_backreference_does_not_reorder_the_story(self):
        spec = candidate()
        spec["trajectory"][1]["refs"] = ["E000001", "E000006"]
        original_order = [entry["title"] for entry in spec["trajectory"]]
        self.assertEqual([entry["title"] for entry in order_spec(spec)["trajectory"]], original_order)

    def test_fabricated_quote_rejected(self):
        spec = candidate()
        spec["requirements"][0]["quote"] = "User approved deleting everything"
        self.assertTrue(validate_spec(spec, self.records))

    def test_quote_is_mandatory_only_for_user_attribution(self):
        spec = candidate()
        spec["requirements"][0].pop("quote")
        self.assertTrue(validate_spec(spec, self.records))
        spec["requirements"][0]["attribution"] = "inferred"
        self.assertFalse(validate_spec(spec, self.records))

    def test_assistant_claim_cannot_be_verified(self):
        spec = candidate()
        spec["this_run"]["work"][0]["status"] = "verified"
        self.assertTrue(validate_spec(spec, self.records))

    def test_missing_last_turn_rejected(self):
        spec = candidate()
        spec["request_coverage"].pop()
        self.assertTrue(validate_spec(spec, self.records))

    def test_supersession_needs_later_human_evidence(self):
        spec = candidate()
        spec["requirements"][0]["status"] = "superseded"
        self.assertTrue(validate_spec(spec, self.records))
        spec["requirements"][0].update({"quote": "Export CSV and JSON", "refs": ["E000002", "E000006"]})
        self.assertFalse(validate_spec(spec, self.records))

    def test_invented_evidence_rejected(self):
        spec = candidate()
        spec["trajectory"][0]["refs"] = ["E999999"]
        self.assertTrue(validate_spec(spec, self.records))

    def test_bad_reference_types_reported_without_crashing(self):
        spec = candidate()
        spec["requirements"][0]["refs"] = None
        self.assertTrue(validate_spec(spec, self.records))

    def test_failed_tool_not_proof_of_success(self):
        records = copy.deepcopy(self.records)
        next(record for record in records if record["ref"] == "E000004")["success"] = False
        self.assertTrue(validate_spec(candidate(), records))

    def test_proven_reuse_requires_observation(self):
        spec = candidate()
        spec["next_run"]["reuse"][0]["basis"] = "observed_success"
        self.assertTrue(validate_spec(spec, self.records))

    def test_future_explicit_requirement_needs_user_evidence(self):
        spec = candidate()
        spec["next_run"]["acceptance"][0]["refs"] = ["E000005"]
        self.assertTrue(validate_spec(spec, self.records))

    def test_future_authority_is_only_downgraded_never_fabricated(self):
        spec = candidate()
        spec["next_run"]["acceptance"][0]["refs"] = ["E000005"]
        original = copy.deepcopy(spec["next_run"]["acceptance"][0])
        warnings = calibrate_next_basis(spec, self.records)
        self.assertEqual(len(warnings), 1)
        actual = spec["next_run"]["acceptance"][0]
        self.assertEqual(actual, dict(original, basis="proposed"))
        self.assertFalse(validate_spec(spec, self.records))

    def test_output_views_have_identical_canonical_hash(self):
        report = {"status": "reviewed_draft", "warnings": [], "review": {}}
        human = lines_for_spec(candidate(), self.metadata, report)
        agent = lines_for_spec(candidate(), self.metadata, report, agent=True)
        self.assertEqual(human.splitlines()[3], agent.splitlines()[3])
        self.assertNotEqual(human, agent)
        for text in (human, agent):
            self.assertIn("postpone JSON", text)
            self.assertIn("[E000006](evidence.md#e000006)", text)

    def test_unresolved_errors_block_execution_in_both_views(self):
        report = {"status": "needs_review", "warnings": [], "review": {}}
        for agent in (False, True):
            rendered = lines_for_spec(candidate(), self.metadata, report, agent=agent)
            self.assertIn("不要据此直接执行下一步", rendered)


class BackendTests(unittest.TestCase):
    def test_multi_message_json_assembled_only_when_exactly_valid(self):
        for pieces, expected, selection in [
            (['{"text":', '"complete"}'], {"text": "complete"}, "joined_complete_json"),
            (["Preparing result", '{"ok":true}'], {"ok": True}, "last_message"),
            (['{"draft":true}', '{"ok":true}'], {"ok": True}, "last_message"),
        ]:
            output = "\n".join(json.dumps({"type": "assistant.message", "data": {"content": piece}}) for piece in pieces)
            response, receipt = response_from_events(output)
            self.assertEqual(parse_json(response), expected)
            self.assertEqual(receipt["response_selection"], selection)
            self.assertEqual(receipt["message_lengths"], [len(piece) for piece in pieces])

    def test_surface_repair_preserves_string_content(self):
        value, repairs = decode_json('{refs:["E000001",],"text":"literal ,refs: text"')
        self.assertEqual(value, {"refs": ["E000001"], "text": "literal ,refs: text"})
        self.assertEqual(len(repairs), 3)

    def test_truncated_values_and_duplicates_are_not_invented(self):
        for malformed in ['{"text":"unfinished', '{"text":', '{"text":"a","text":"b"}', '{"text":function(){}}']:
            with self.subTest(malformed=malformed), self.assertRaises(ValueError):
                parse_json(malformed)

    def test_json_fences_and_nonobjects(self):
        self.assertEqual(parse_json('```json\n{"ok":true}\n```'), {"ok": True})
        with self.assertRaises(ValueError):
            parse_json("[]")
        with self.assertRaises(ValueError):
            parse_json('Some prose {"ok":true}')

    def test_tool_calls_fail_closed(self):
        output = json.dumps({"type": "assistant.message", "data": {"content": "{}", "toolRequests": [{"name": "shell"}]}})
        with self.assertRaises(ValueError):
            response_from_events(output)

    def test_jsonl_final_response_not_initialization_noise(self):
        output = '\n'.join([
            json.dumps({"type": "session.skills_loaded", "data": {"skills": []}}),
            json.dumps({"type": "assistant.message", "data": {"content": '{"ok":true}'}}),
            json.dumps({"type": "result", "usage": {"premiumRequests": 1}}),
        ])
        response, receipt = response_from_events(output)
        self.assertEqual(parse_json(response), {"ok": True})
        self.assertEqual(receipt["tool_calls"], 0)


class PatchTests(unittest.TestCase):
    def test_patch_changes_only_the_selected_data(self):
        original = candidate()
        result = apply_spec_patches(original, {"patches": [
            {"op": "replace", "path": "/requirements/0/status", "value": "unclear"},
            {"op": "add", "path": "/next_run/steps/-", "value": copy.deepcopy(original["next_run"]["steps"][0])},
            {"op": "remove", "path": "/open_questions/0"},
        ]})
        self.assertEqual(original, candidate())
        self.assertEqual(result["requirements"][0]["status"], "unclear")
        self.assertEqual(result["trajectory"], original["trajectory"])
        self.assertEqual(len(result["next_run"]["steps"]), 2)
        self.assertEqual(result["open_questions"], [])

    def test_invalid_patch_is_atomic_and_cannot_target_external_state(self):
        original = candidate()
        for path in ("", "/source/path", "/requirements/-1/status", "/requirements/999/status", "/requirements/~2", "/title/child"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                apply_spec_patches(original, {"patches": [
                    {"op": "replace", "path": "/title", "value": "changed"},
                    {"op": "replace", "path": path, "value": "bad"},
                ]})
            self.assertEqual(original, candidate())
        with self.assertRaises(ValueError):
            apply_spec_patches(original, {"patches": [{"op": "execute", "path": "/title", "value": "anything"}]})


class PipelineTests(unittest.TestCase):
    def test_pipeline_repairs_a_quote_with_a_local_patch_then_revalidates(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "copilot"
            path = write_session(home)
            output = Path(temporary) / "export"

            class PatchBackend:
                def __init__(self, **options):
                    self.calls = []

                def generate(self, prompt, label):
                    self.calls.append({"label": label})
                    if label.startswith("repair-patch"):
                        return {"patches": [{"op": "replace", "path": "/requirements/0/quote", "value": "CSV only"}]}
                    if label.startswith("review"):
                        return {"issues": [], "limitations": []}
                    result = copy.deepcopy(candidate())
                    if label == "synthesize":
                        result["requirements"][0]["quote"] = "Paraphrase, not a real quote"
                    return result

            result = run_export(str(path), home, output, backend_factory=PatchBackend)
            self.assertEqual(result["status"], "reviewed_draft")
            wrapper = json.loads((output / "spec.json").read_text(encoding="utf-8"))
            self.assertEqual(wrapper["spec"], candidate())
            report = json.loads((output / "report.json").read_text(encoding="utf-8"))
            self.assertIn("repair-patch-1", [entry["label"] for entry in report["calls"]])
            self.assertNotIn("repair-1", [entry["label"] for entry in report["calls"]])

    def test_failed_resume_preserves_previously_published_views_and_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "copilot"
            path = write_session(home)
            output = Path(temporary) / "export"

            class UnreliableBackend:
                fail = False

                def __init__(self, **options):
                    self.calls = []

                def generate(self, prompt, label):
                    if self.fail:
                        raise ValueError("Simulated provider failure")
                    self.calls.append({"label": label})
                    return {"issues": [], "limitations": []} if label.startswith("review") else copy.deepcopy(candidate())

            run_export(str(path), home, output, backend_factory=UnreliableBackend)
            original = {name: (output / name).read_bytes() for name in ("report.json", "human-spec.md", "agent-spec.md", "spec.json")}
            UnreliableBackend.fail = True
            with self.assertRaises(ValueError):
                run_export(str(path), home, output, resume=True, model="uncached-model", backend_factory=UnreliableBackend)
            self.assertTrue(all((output / name).read_bytes() == content for name, content in original.items()))
            attempt = json.loads((output / "attempt-report.json").read_text(encoding="utf-8"))
            self.assertEqual(attempt["status"], "failed")
            self.assertTrue(attempt["previous_published_specs_preserved"])

    def test_intent_errors_remain_visible_even_when_evidence_review_passes(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "copilot"
            path = write_session(home)
            output = Path(temporary) / "export"

            class DisagreeingReviewBackend:
                def __init__(self, **options):
                    self.calls = []

                def generate(self, prompt, label):
                    self.calls.append({"label": label})
                    if label.startswith("review-intent"):
                        return {"issues": [{"severity": "error", "section": "requirements", "message": "Concrete intent contradiction", "refs": ["E000006"]}], "limitations": []}
                    return {"issues": [], "limitations": []} if label.startswith("review") else copy.deepcopy(candidate())

            result = run_export(str(path), home, output, backend_factory=DisagreeingReviewBackend)
            self.assertEqual(result["status"], "needs_review")
            report = json.loads((output / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["review"]["stages"], ["intent", "evidence"])
            self.assertEqual(report["review"]["issues"][0]["review_stage"], "intent")
            self.assertIn("Concrete intent contradiction", (output / "human-spec.md").read_text(encoding="utf-8"))

    def test_failed_shape_retry_does_not_poison_resume_cache(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "copilot"
            path = write_session(home)
            output = Path(temporary) / "export"

            class RecoveringBackend:
                fail_shape = True

                def __init__(self, **options):
                    self.calls = []

                def generate(self, prompt, label):
                    self.calls.append({"label": label})
                    if label == "synthesize" or (label.startswith("shape-repair") and self.fail_shape):
                        return {"ref": "E000002"}
                    return {"issues": [], "limitations": []} if label.startswith("review") else copy.deepcopy(candidate())

            with self.assertRaises(ValueError):
                run_export(str(path), home, output, backend_factory=RecoveringBackend)
            RecoveringBackend.fail_shape = False
            result = run_export(str(path), home, output, resume=True, rebuild=True, backend_factory=RecoveringBackend)
            self.assertEqual(result["status"], "reviewed_draft")
            self.assertEqual(result["cached_calls"], 2)

    def test_review_fallback_keeps_results_without_candidate_refs(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            path = write_session(home)
            _, records = read_session(path, home)
            requests, evidence, omitted = review_evidence({}, records)
            self.assertEqual(len(requests), 2)
            self.assertEqual({record["ref"] for record in evidence}, {"E000004", "E000005"})
            self.assertEqual(omitted, 0)

    def test_schema_fragment_retried_with_original_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "copilot"
            path = write_session(home)
            output = Path(temporary) / "export"

            class FragmentBackend:
                def __init__(self, **options):
                    self.calls = []

                def generate(self, prompt, label):
                    self.calls.append({"label": label})
                    if label == "synthesize":
                        return {"ref": "E000002", "summary": "Only a fragment"}
                    if label == "shape-repair-synthesize":
                        if "2 tests passed" not in prompt:
                            raise AssertionError("Recovery lost historical observations")
                    return {"issues": [], "limitations": []} if label.startswith("review") else copy.deepcopy(candidate())

            result = run_export(str(path), home, output, backend_factory=FragmentBackend)
            self.assertEqual(result["status"], "reviewed_draft")
            self.assertTrue((output / "synthesize-schema-invalid.json").exists())
            self.assertFalse(usable_spec_shape({"ref": "E000002"}))
            self.assertTrue(usable_spec_shape(candidate()))
            (output / "candidate-invalid.json").write_text('{"ref":"E000002"}')
            repeated = run_export(str(path), home, output, resume=True, backend_factory=FragmentBackend)
            self.assertEqual(repeated["status"], "reviewed_draft")
            self.assertEqual(repeated["calls"], 0)

    def test_concurrent_export_lock_is_released(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            with export_lock(directory):
                with self.assertRaises(ValueError):
                    with export_lock(directory):
                        self.fail("Concurrent lock unexpectedly succeeded")
            with export_lock(directory):
                self.assertTrue((directory / ".export.lock").exists())

    def test_end_to_end_cache_readonly_and_projection_tamper(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "copilot"
            path = write_session(home)
            original = path.read_bytes()
            output = Path(temporary) / "export"
            prompts = []

            class FakeBackend:
                def __init__(self, **options):
                    self.calls = []

                def generate(self, prompt, label):
                    self.calls.append({"label": label})
                    prompts.append(prompt)
                    return {"issues": [], "limitations": []} if label.startswith("review") else copy.deepcopy(candidate())

            result = run_export(str(path), home, output, backend_factory=FakeBackend)
            self.assertEqual(result["status"], "reviewed_draft")
            self.assertTrue(any("SOURCE_LANGUAGE_POLICY" in prompt for prompt in prompts))
            self.assertTrue(all("The output language is" not in prompt for prompt in prompts))
            self.assertEqual(json.loads((output / "report.json").read_bytes())["requested_language"], "auto")
            self.assertEqual(path.read_bytes(), original)
            repeated = run_export(str(path), home, output, resume=True, backend_factory=FakeBackend)
            self.assertEqual(repeated["calls"], 0)
            self.assertEqual(repeated["cached_calls"], 3)
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(["validate", str(output)]), 0)
            with (output / "human-spec.md").open("a", encoding="utf-8") as stream:
                stream.write("Fabricated result")
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(["validate", str(output)]), 1)
            with self.assertRaises(ValueError):
                run_export(str(path), home, output, backend_factory=FakeBackend)

    def test_source_directory_cannot_be_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            path = write_session(home)
            with self.assertRaises(ValueError):
                run_export(str(path), home, path.parent)


if __name__ == "__main__":
    unittest.main()
