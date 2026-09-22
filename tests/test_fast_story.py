import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from offline_provider import guard_offline_test
from session_spec.backend import ModelResponseError, PreparationTimeoutError
from session_spec.fast_story import PROFILE, SCHEMA, STATUS, DraftValidationError, apply_replacements, brief_authority_claims, handoff_attention, normalize_edition_envelope, run_fast_story, source_packet, structural_issues, validate_fast_story
from session_spec.storage import file_hash, write_json
from session_spec.story_draft import generate_draft
from test_story_pipeline import FakeBackend, article, brief, insights, packet


def draft():
    value = {"article": article(), "brief": brief(), "insights": insights()}
    value["article"]["agent_markdown"] = value["article"]["agent_markdown"].replace("_support/evidence.jsonl", "evidence.md")
    return value


@unittest.skipUnless(shutil.which("node"), "Node.js is required for the real deterministic renderer")
class FastStoryTests(unittest.TestCase):
    def setUp(self):
        guard_offline_test(self)
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.base = self.root / "canonical"
        self.base.mkdir()
        self.source = self.root / "source/events.jsonl"
        self.source.parent.mkdir()
        self.source.write_bytes(b'{"fixture":"Synthetic source; no executed task or model"}\r\n')
        self.metadata = {"source_path": str(self.source), "snapshot_bytes": self.source.stat().st_size,
                         "source_sha256": file_hash(self.source)}
        write_json(self.base / "source.json", self.metadata)
        self.events = packet()
        self.write_records()
        self.output = self.root / "private-draft"

    def tearDown(self):
        self.temporary.cleanup()

    def write_records(self):
        (self.base / "evidence.jsonl").write_bytes("".join(json.dumps(event, ensure_ascii=False) + "\n" for event in self.events).encode())

    def test_one_fake_call_real_render_and_distinct_structural_only_receipt(self):
        backend = FakeBackend([draft()])
        with patch("session_spec.story_editor.generate_edition", side_effect=AssertionError("No experimental editor")), \
                patch("session_spec.transfer_probe.run_probe", side_effect=AssertionError("No fresh reader")):
            result = run_fast_story(self.base, self.output, backend)
        self.assertEqual(result["model_calls"], 1)
        self.assertEqual([call["label"] for call in backend.calls], ["fast-story-draft"])
        report = json.loads((self.output / "_support/story-report.json").read_bytes())
        self.assertEqual((report["schema"], report["profile"], report["status"]), (SCHEMA, PROFILE, STATUS))
        self.assertEqual(report["semantic_review"], "pending")
        self.assertNotIn("output_schema", report)
        self.assertNotIn("final_transfer_policy", report["identity"])
        self.assertEqual(report["evidence_renderer"], "companion/v8")
        validation = validate_fast_story(self.output)
        self.assertTrue(validation["valid"], validation)
        self.assertEqual(validation["semantic_review"], "pending")
        self.assertEqual((self.output / "_support/evidence.jsonl").read_bytes(), (self.base / "evidence.jsonl").read_bytes())
        self.assertEqual((self.output / "_support/source.json").read_bytes(), (self.base / "source.json").read_bytes())
        self.assertIn(b"startup signal observed", (self.output / "evidence.md").read_bytes())
        self.assertTrue((self.output / "human-spec.html").read_bytes().lower().startswith(b"<!doctype html>"))

    def test_attention_keeps_literal_hazard_navigation_without_mutating_or_verifying_source(self):
        records = [
            {"ref": "E000001", "type": "assistant.message", "text": "Reported hardcoded credential; remove/rotate it after inspection."},
            {"ref": "E000002", "type": "assistant.message", "text": "Archived check: skip tests if unavailable; os.Exit(0)."},
            {"ref": "E000003", "type": "tool.execution_complete", "result": "Edit rejected by the user."},
        ]
        original = copy.deepcopy(records)
        result = handoff_attention(records)
        self.assertEqual(records, original)
        self.assertEqual([item["ref"] for item in result["candidates"]], ["E000001", "E000002", "E000003"])
        for candidate, source in zip(result["candidates"], records):
            self.assertIn(candidate["quote"], source.get("text", source.get("result")))
            self.assertEqual(candidate["type"], source["type"])
        self.assertIn("not verified risks", result["scope"])
        large = handoff_attention(records * 10)
        self.assertEqual(len(large["candidates"]), 12)
        self.assertEqual(large["additional_candidates"], 18)

    def test_single_structural_repair_keeps_all_inputs_and_human_agent_reference_boundary(self):
        invalid = draft()
        invalid["article"]["chapters"][0]["markdown"] += " E000002"
        invalid["article"]["human_input_coverage"] = []
        replacements = {"replacements": [
            {"path": ["article", "chapters", 0, "markdown"], "value": draft()["article"]["chapters"][0]["markdown"]},
            {"path": ["article", "human_input_coverage"], "value": draft()["article"]["human_input_coverage"]},
        ]}
        backend = FakeBackend([invalid, replacements])
        run_fast_story(self.base, self.output, backend)
        self.assertEqual(len(backend.calls), 2)
        self.assertEqual(backend.calls[-1]["label"], "fast-story-repair")
        self.assertIn("ONE_FINAL_STRUCTURAL_REPAIR", backend.prompts[-1])
        self.assertIn("Human prose has no E IDs, Agent citations remain", backend.prompts[-1])
        receipt = json.loads((self.output / "_support/fast-receipt.json").read_bytes())
        self.assertEqual(len(receipt["attempts"]), 2)
        self.assertTrue(receipt["attempts"][0]["issues"])
        self.assertEqual(receipt["attempts"][1]["issues"], [])
        self.assertEqual(receipt["attempts"][1]["repair_mode"], "field_replacements")
        self.assertEqual(json.loads((self.output / "_support/fast-replacements-1.json").read_bytes()), replacements)
        self.assertTrue(validate_fast_story(self.output)["valid"])

    def test_unambiguous_nested_roots_move_without_model_rewrite_or_content_changes(self):
        nested = draft()
        for name in ("brief", "insights"):
            nested["article"][name] = nested.pop(name)
        original = copy.deepcopy(nested)
        backend = FakeBackend([nested])
        run_fast_story(self.base, self.output, backend)
        support = self.output / "_support"
        self.assertEqual(len(backend.calls), 1)
        self.assertEqual(nested, original)
        self.assertEqual(json.loads((support / "fast-response-0.json").read_bytes()), original)
        self.assertEqual(json.loads((support / "fast-candidate-0.json").read_bytes()), draft())
        receipt = json.loads((support / "fast-receipt.json").read_bytes())
        self.assertEqual(receipt["attempts"][0]["envelope_moves"], [
            {"from": ["article", "brief"], "to": ["brief"]},
            {"from": ["article", "insights"], "to": ["insights"]}])
        self.assertTrue(validate_fast_story(self.output)["valid"])
        write_json(support / "fast-response-0.json", draft())
        self.assertFalse(validate_fast_story(self.output)["valid"])

    def test_envelope_normalization_never_resolves_conflicts_or_unknown_roots(self):
        conflicting = draft()
        conflicting["article"]["brief"] = copy.deepcopy(conflicting["brief"])
        extra = {"article": {"brief": draft()["brief"]}, "unexpected": "retained"}
        unknown = {"article": {"brief": {"schema": "invented", "text": "retained"}}}
        for candidate in (conflicting, extra, unknown, None, [draft()]):
            with self.subTest(candidate=candidate):
                original = copy.deepcopy(candidate)
                normalized, moves = normalize_edition_envelope(candidate)
                self.assertEqual(normalized, original)
                self.assertEqual(candidate, original)
                self.assertEqual(moves, [])
                self.assertTrue(structural_issues(normalized, self.events, "auto"))

    def test_unpatchable_missing_root_gets_one_complete_response_not_impossible_replacements(self):
        malformed = {"article": draft()["article"]}
        backend = FakeBackend([malformed, draft()])
        run_fast_story(self.base, self.output, backend)
        self.assertEqual(len(backend.calls), 2)
        receipt = json.loads((self.output / "_support/fast-receipt.json").read_bytes())
        self.assertEqual(receipt["attempts"][1]["repair_mode"], "complete_json")
        self.assertTrue(validate_fast_story(self.output)["valid"])

    def test_format_failure_consumes_same_repair_allowance_not_an_extra_retry_loop(self):
        backend = FakeBackend([ModelResponseError("Invalid JSON", "{bad json"), draft()])
        run_fast_story(self.base, self.output, backend)
        self.assertEqual(len(backend.calls), 2)
        self.assertTrue((self.output / "_support/fast-invalid-response-0.json").is_file())
        self.assertIn("invalid JSON/root envelope that field replacements cannot fix; return one complete corrected joint JSON", backend.prompts[-1])
        self.assertEqual(json.loads((self.output / "_support/fast-receipt.json").read_bytes())["attempts"][1]["repair_mode"], "complete_json")
        self.assertTrue(validate_fast_story(self.output)["valid"])

    def test_second_invalid_candidate_fails_closed_and_keeps_both_attempts(self):
        invalid = draft()
        invalid["article"]["agent_detail"]["trajectory"][0]["tool_refs"] = ["E000001"]
        replacements = {"replacements": [{"path": ["article", "agent_detail", "trajectory", 0, "tool_refs"], "value": ["E000001"]}]}
        backend = FakeBackend([invalid, replacements, draft()])
        with self.assertRaisesRegex(ValueError, "one bounded structural repair"):
            run_fast_story(self.base, self.output, backend)
        self.assertEqual(len(backend.calls), 2)
        self.assertFalse((self.output / "_support/story-report.json").exists())
        self.assertFalse((self.output / "agent-spec.md").exists())
        receipt = json.loads((self.output / "_support/fast-attempt.json").read_bytes())
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(len(receipt["attempts"]), 2)
        self.assertTrue((self.output / "_support/fast-candidate-0.json").is_file())
        self.assertTrue((self.output / "_support/fast-candidate-1.json").is_file())

    def test_empty_continuation_refs_are_repaired_only_by_model_supplied_exact_field(self):
        invalid = draft()
        invalid["article"]["agent_detail"]["continuation"][0]["steps"][0]["refs"] = []
        path = ["article", "agent_detail", "continuation", 0, "steps", 0, "refs"]
        response = {"replacements": [{"path": path, "value": ["E000002"]}]}
        backend = FakeBackend([invalid, response])
        run_fast_story(self.base, self.output, backend)
        support = self.output / "_support"
        self.assertEqual(json.loads((support / "edition.json").read_bytes()), draft())
        self.assertEqual(json.loads((support / "fast-candidate-0.json").read_bytes()), invalid)
        self.assertEqual(len(backend.calls), 2)
        self.assertIn('"candidate":', backend.prompts[1])
        self.assertIn('"issues":', backend.prompts[1])
        self.assertIn("COMPLETE_OBSERVABLE_SOURCE_DATA", backend.prompts[1])
        self.assertIn("startup signal observed", backend.prompts[1])
        self.assertIn("EVERY continuation step", backend.prompts[0])
        self.assertTrue(validate_fast_story(self.output)["valid"])

    def test_missing_continuation_refs_use_existing_parent_repair_without_invented_evidence(self):
        for suffix in ([], ["steps", 0]):
            with self.subTest(suffix=suffix):
                expected = draft()
                invalid = copy.deepcopy(expected)
                path = ["article", "agent_detail", "continuation", 0, *suffix]
                target, replacement = invalid, expected
                for part in path:
                    target, replacement = target[part], replacement[part]
                target.pop("refs")
                issues = structural_issues(invalid, self.events, "auto")
                pointer = "/" + "/".join(str(part) for part in path)
                self.assertTrue(any(pointer in issue and "refs is missing" in issue for issue in issues), issues)
                backend = FakeBackend([invalid, {"replacements": [{"path": path, "value": replacement}]}])
                destination = self.root / ("missing-step-refs" if suffix else "missing-parent-refs")
                run_fast_story(self.base, destination, backend)
                self.assertIn("nearest EXISTING parent object", backend.prompts[1])
                self.assertIn("child refs do not substitute for parent refs", backend.prompts[0])
                self.assertEqual(json.loads((destination / "_support/edition.json").read_bytes()), expected)
                self.assertEqual(json.loads((destination / "_support/fast-candidate-0.json").read_bytes()), invalid)
                self.assertEqual(len(backend.calls), 2)
                self.assertTrue(validate_fast_story(destination)["valid"])
                with self.assertRaisesRegex(ValueError, "existing field"):
                    apply_replacements(invalid, {"replacements": [{"path": [*path, "refs"], "value": replacement["refs"]}]})

    def test_brief_shape_repair_receives_exact_keys_kinds_and_source_provenance(self):
        invalid = draft()
        constraint = invalid["brief"]["constraints"][0]
        constraint["category"] = constraint.pop("kind")
        response = {"replacements": [{"path": ["brief", "constraints", 0], "value": draft()["brief"]["constraints"][0]}]}
        backend = FakeBackend([invalid, response])
        run_fast_story(self.base, self.output, backend)
        self.assertEqual(len(backend.calls), 2)
        self.assertIn("/brief/constraints/0/kind", backend.prompts[1])
        self.assertIn("expected one of", backend.prompts[1])
        self.assertIn("expected an object with exactly keys", backend.prompts[1])
        self.assertIn("Choose from source provenance", backend.prompts[1])
        self.assertEqual(json.loads((self.output / "_support/edition.json").read_bytes()), draft())
        self.assertEqual(json.loads((self.output / "_support/fast-candidate-0.json").read_bytes()), invalid)
        self.assertTrue(validate_fast_story(self.output)["valid"])

    def test_reported_rejection_repair_preserves_real_refs_and_describes_history(self):
        self.events.append({"ref": "E000003", "type": "assistant.message", "origin": "root",
                            "text": "Archived tool output: the user rejected this edit; replacement was not written. STOP and wait."})
        self.write_records()
        invalid = draft()
        invalid["brief"]["constraints"].append({"kind": "requirement", "text": "未经用户批准不得修改入口。", "refs": ["E000003"]})
        fixed = {"kind": "environment", "text": "归档工具报告该次修改被拒绝，替换内容未写入。", "refs": ["E000003"]}
        response = {"replacements": [{"path": ["brief", "constraints", 1], "value": fixed}]}
        backend = FakeBackend([invalid, response])
        run_fast_story(self.base, self.output, backend)
        support = self.output / "_support"
        original = json.loads((support / "fast-candidate-0.json").read_bytes())
        repaired = json.loads((support / "edition.json").read_bytes())
        self.assertEqual(original, invalid)
        self.assertEqual(repaired["brief"]["constraints"][1], fixed)
        self.assertEqual(repaired["brief"]["constraints"][1]["refs"], original["brief"]["constraints"][1]["refs"])
        self.assertEqual(len(backend.calls), 2)
        self.assertIn("assistant/tool reports are not user intent", backend.prompts[1])
        self.assertTrue(validate_fast_story(self.output)["valid"])

    def test_claim_navigation_exposes_unrelated_human_citation_without_rewriting_source(self):
        records = [{"ref": "E000001", "type": "user.message", "origin": "root",
                    "human_input": "Is the matrix parallel?"},
                   {"ref": "E000002", "type": "assistant.message", "origin": "root",
                    "text": "Archived tool report: the edit was rejected."}]
        value = {"constraints": [{"kind": "requirement", "text": "Never edit without approval.", "refs": ["E000001"]},
                                  {"kind": "environment", "text": "The report says the edit was rejected.", "refs": ["E000002"]}]}
        original = copy.deepcopy((value, records))
        claims = brief_authority_claims(value, records)
        self.assertEqual(claims[0]["text"], "Never edit without approval.")
        self.assertEqual(claims[0]["cited_sources"][0]["human_input_excerpt"], "Is the matrix parallel?")
        self.assertTrue(claims[0]["cited_sources"][0]["has_human_input"])
        self.assertFalse(claims[1]["cited_sources"][0]["has_human_input"])
        self.assertEqual(claims[1]["cited_sources"][0]["type"], "assistant.message")
        self.assertEqual((value, records), original)
        records[0]["human_input"] += "x" * 1300
        bounded = brief_authority_claims(value, records)[0]["cited_sources"][0]
        self.assertEqual(bounded["human_input_excerpt"], records[0]["human_input"][:1200])
        self.assertTrue(bounded["excerpt_truncated"])

    def test_replacements_reject_unsafe_protocol_bounds_paths_and_conflicts_atomically(self):
        candidate = draft()
        original = copy.deepcopy(candidate)
        good = {"path": ["article", "title"], "value": "Replacement"}
        cases = [None, draft(), {"replacements": []}, {"replacements": [good] * 9},
                 {"replacements": [good], "extra": True},
                 {"replacements": [{**good, "value": "x" * 6001}]},
                 {"replacements": [{**good, "op": "replace"}]},
                 {"replacements": [good, good]},
                 {"replacements": [{"path": ["article", "agent_detail"], "value": {}},
                                   {"path": ["article", "agent_detail", "resume"], "value": {}}]}]
        for path in ([], ["article"], ["source", "text"], ["article", "missing"],
                     ["article", "chapters", -1], ["article", "chapters", 99], ["article", "chapters", True],
                     ["article", "chapters", "0"], ["article", 0], ["article", "title", "child"],
                     ["article", "chapters", {}], ["article"] * 17):
            cases.append({"replacements": [good, {"path": path, "value": "bad"}]})
        for response in cases:
            with self.subTest(response=response), self.assertRaises(ValueError):
                apply_replacements(candidate, response)
            self.assertEqual(candidate, original)

    def test_unknown_repaired_refs_are_not_published_or_retried(self):
        invalid = draft()
        invalid["article"]["agent_detail"]["continuation"][0]["steps"][0]["refs"] = []
        response = {"replacements": [{"path": ["article", "agent_detail", "continuation", 0, "steps", 0, "refs"], "value": ["E999999"]}]}
        backend = FakeBackend([invalid, response, draft()])
        with self.assertRaisesRegex(ValueError, "one bounded structural repair"):
            run_fast_story(self.base, self.output, backend)
        self.assertEqual(len(backend.calls), 2)
        self.assertFalse((self.output / "_support/story-report.json").exists())
        self.assertFalse((self.output / "agent-spec.md").exists())

    def test_parsed_invalid_draft_rejects_full_regeneration_and_retains_response(self):
        invalid = draft()
        invalid["article"]["agent_detail"]["continuation"][0]["steps"][0]["refs"] = []
        backend = FakeBackend([invalid, draft(), draft()])
        with self.assertRaisesRegex(DraftValidationError, "invalid replacements") as raised:
            run_fast_story(self.base, self.output, backend)
        self.assertEqual(raised.exception.error_code, "draft_references_invalid")
        self.assertEqual(len(backend.calls), 2)
        self.assertFalse((self.output / "_support/story-report.json").exists())
        self.assertEqual(json.loads((self.output / "_support/fast-replacements-1.json").read_bytes()), draft())
        self.assertEqual(json.loads((self.output / "_support/fast-attempt.json").read_bytes())["status"], "failed")

    def test_invalid_json_exhausts_one_repair_then_reports_a_safe_format_category(self):
        backend = FakeBackend([ModelResponseError("bad", "PRIVATE_INVALID"), ModelResponseError("bad", "PRIVATE_REPAIR")])
        with self.assertRaises(DraftValidationError) as raised:
            run_fast_story(self.base, self.output, backend)
        self.assertEqual(raised.exception.error_code, "draft_structure_invalid")
        self.assertNotIn("PRIVATE", str(raised.exception))
        self.assertEqual(len(backend.calls), 2)
        self.assertFalse((self.output / "agent-spec.md").exists())

    def test_provider_timeout_or_auth_failure_never_consumes_a_repair_or_renders(self):
        for index, failure in enumerate((TimeoutError("Synthetic deadline"), ValueError("Synthetic auth failure"))):
            destination = self.root / f"failure-{index}"
            backend = FakeBackend([failure, draft()])
            with patch("session_spec.fast_story.render_story", side_effect=AssertionError("No rendering")), self.assertRaises(type(failure)):
                run_fast_story(self.base, destination, backend)
            self.assertEqual(len(backend.calls), 1)
            self.assertEqual(json.loads((destination / "_support/fast-attempt.json").read_bytes())["status"], "failed")

    def test_source_changes_and_existing_output_are_refused_without_model_or_overwrite(self):
        backend = FakeBackend([draft()])
        original = self.source.read_bytes()
        self.source.write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "snapshot hash"):
            run_fast_story(self.base, self.output, backend)
        self.assertEqual(backend.calls, [])
        self.assertFalse(self.output.exists())
        self.source.write_bytes(original)
        self.output.mkdir()
        sentinel = self.output / "prior-output"
        sentinel.write_bytes(b"preserve")
        with self.assertRaisesRegex(ValueError, "fresh fast-story"):
            run_fast_story(self.base, self.output, backend)
        self.assertEqual(sentinel.read_bytes(), b"preserve")
        self.assertEqual(backend.calls, [])

    def test_complete_records_retain_delegated_archives_without_promoting_user_or_tool_roles(self):
        records = self.events + [{"ref": "E000003", "origin": "delegated", "type": "assistant.message", "text": "ARCHIVE_HEAD " + "middle " * 1000 + " ARCHIVE_TAIL", "human_input": "not root authorization"}]
        projected = source_packet(records)
        self.assertEqual(len(projected), len(records))
        self.assertEqual(projected[-1]["text"], records[-1]["text"])
        self.assertNotIn("human_input", projected[-1])
        self.assertEqual(projected[-1]["origin"], "delegated")
        self.events = records
        self.write_records()
        backend = FakeBackend([draft()])
        run_fast_story(self.base, self.output, backend)
        self.assertIn("ARCHIVE_TAIL", backend.prompts[0])
        self.assertIn("quoted inside an assistant message", backend.prompts[0])
        self.assertEqual(len(json.loads((self.output / "_support/input.json").read_bytes())), len(records))

    def test_archived_tool_text_cannot_satisfy_machine_tool_references(self):
        events = packet()
        events[1] = {"ref": "E000002", "origin": "root", "type": "assistant.message", "text": "Reported shell readback: startup signal observed"}
        errors = structural_issues(draft(), events, "auto")
        self.assertTrue(any("real tool" in issue for issue in errors))
        self.assertTrue(any("completion/readback" in issue for issue in errors))

    def test_supported_architecture_is_retained_and_prompt_allows_evidence_based_inclusion(self):
        backend = FakeBackend([draft()])
        run_fast_story(self.base, self.output, backend)
        edition = json.loads((self.output / "_support/edition.json").read_bytes())
        self.assertEqual(edition["insights"]["architecture"], draft()["insights"]["architecture"])
        self.assertEqual(edition["insights"]["architecture"]["decision"], "include")
        self.assertIn("Architecture is conditional, not disabled for compactness", backend.prompts[0])
        self.assertIn('"decision":"include"', backend.prompts[0])
        self.assertIn("assistant-quoted archives, requests and proposals alone cannot establish", backend.prompts[0])
        self.assertIn("Never describe a user's self-criticism as the assistant's", backend.prompts[0])
        self.assertIn("Do not turn a closing personal/privacy aside into a technical non-goal", backend.prompts[0])
        self.assertTrue(validate_fast_story(self.output)["valid"])

    def test_validation_preserves_terminal_timeout_and_cleanup_markers(self):
        run_fast_story(self.base, self.output, FakeBackend([draft()]))
        for confirmed in (True, False):
            error = PreparationTimeoutError(cleanup_confirmed=confirmed)
            with self.subTest(cleanup_confirmed=confirmed), \
                    patch("session_spec.fast_story.render_story", side_effect=error), \
                    self.assertRaises(PreparationTimeoutError) as raised:
                validate_fast_story(self.output)
            self.assertIs(raised.exception, error)
            self.assertEqual(raised.exception.error_code, "timeout" if confirmed else "cleanup_unconfirmed")

    def test_source_and_published_pair_tamper_fail_even_if_output_hash_is_repainted(self):
        run_fast_story(self.base, self.output, FakeBackend([draft()]))
        report_path = self.output / "_support/story-report.json"
        original_report = report_path.read_bytes()
        for filename in ("human-spec.html", "agent-spec.md", "evidence.md"):
            target = self.output / filename
            original = target.read_bytes()
            target.write_bytes(original.replace(b"\n", b"\r\n"))
            report = json.loads(original_report)
            report["hashes"][filename] = file_hash(target)
            write_json(report_path, report)
            self.assertFalse(validate_fast_story(self.output)["valid"], filename)
            target.write_bytes(original)
            report_path.write_bytes(original_report)
        self.source.write_bytes(b"changed")
        self.assertFalse(validate_fast_story(self.output)["valid"])

    def test_input_and_receipt_tamper_and_legacy_policy_claims_are_refused(self):
        run_fast_story(self.base, self.output, FakeBackend([draft()]))
        support = self.output / "_support"
        report_path = support / "story-report.json"
        original_report = report_path.read_bytes()
        for filename, replacement in (("input.json", []), ("fast-receipt.json", {"status": "reviewed"}), ("fast-contract.md", "changed")):
            target = support / filename
            original = target.read_bytes()
            write_json(target, replacement)
            report = json.loads(original_report)
            report["support_hashes"][filename] = file_hash(target)
            write_json(report_path, report)
            self.assertFalse(validate_fast_story(self.output)["valid"], filename)
            target.write_bytes(original)
            report_path.write_bytes(original_report)
        report = json.loads(original_report)
        report["semantic_review"] = "passed"
        write_json(report_path, report)
        self.assertFalse(validate_fast_story(self.output)["valid"])

    def test_oversized_source_is_refused_not_silently_truncated(self):
        backend = FakeBackend([draft()])
        with patch("session_spec.fast_story.MAX_INPUT_CHARACTERS", 10), self.assertRaisesRegex(ValueError, "no records were truncated"):
            run_fast_story(self.base, self.output, backend)
        self.assertEqual(backend.calls, [])
        self.assertFalse(self.output.exists())

    def test_prompt_character_baseline_is_measured_without_real_models(self):
        backend = FakeBackend([draft()])
        result = run_fast_story(self.base, self.output, backend)
        old_directory = self.root / "old-draft"
        old_directory.mkdir()
        old = FakeBackend([draft()])
        generate_draft(old_directory, packet(), None, old)
        self.assertLess(len(backend.prompts[0]), len(old.prompts[0]))
        self.assertEqual(result["prompt_characters"], len(backend.prompts[0]))
        self.assertNotIn("PRIOR_REPAIR_FINDINGS", backend.prompts[0])
        print(json.dumps({"fixture": "two synthetic events", "fast_prompt_characters": len(backend.prompts[0]),
                          "legacy_joint_draft_characters": len(old.prompts[0]), "model_calls": 0,
                          "note": "Fake backends only; no measured provider speed or semantic acceptance"}))


if __name__ == "__main__":
    unittest.main()
