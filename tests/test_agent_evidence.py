import copy
import re
import unittest

from session_spec.agent_evidence import EvidenceIndex, literal
from session_spec.agent_handoff import render_agent, tool_ledger, validate_agent_detail
from session_spec.agent_package import render_agent_package
from session_spec.story_editor import validate_edition
from session_spec.story_article import validate_article
from session_spec.story_grounding import complete_reference_pairs
from test_story_pipeline import article, brief, insights, packet


class AgentEvidenceTests(unittest.TestCase):
    def test_accounting_only_aside_cannot_displace_the_rationale_source(self):
        candidate = article()
        reason = "Use the wrapper to avoid changing the existing function."
        aside = "An unrelated greeting about music."
        events = [{"ref": "E000000", "type": "user.message", "text": aside, "human_input": aside}, *packet(),
                  {"ref": "E000003", "type": "assistant.message", "text": reason}]
        candidate["human_input_coverage"].insert(0, {"ref": "E000000", "treatment": "Non-task accounting only."})
        phase = candidate["agent_detail"]["trajectory"][0]
        phase["human_refs"].insert(0, "E000000")
        phase["rationale"] = {"basis": "recorded", "text": reason, "refs": ["E000003"]}
        self.assertEqual(validate_article(candidate, events), [])
        files = render_agent_package(candidate, events, "en")
        rationale_line = next(line for line in files["agent-spec.md"].splitlines() if line.startswith("Rationale ["))
        self.assertIn("evidence.md#e000003", rationale_line)
        self.assertIn("### E000003", files["evidence.md"])
        self.assertIn(reason, files["evidence.md"])
        self.assertNotIn(aside, files["agent-spec.md"])
        legacy = render_agent_package(candidate, events, "en", "handoff-portable-v3")
        self.assertNotIn("### E000003", legacy["evidence.md"])
        self.assertIn(aside, legacy["evidence.md"])
        self.assertEqual(phase["human_refs"], ["E000000", "E000001"])

    def test_claim_citations_do_not_promote_accounting_but_keep_material_short_inputs(self):
        candidate = article()
        aside = "An accounting-only greeting."
        correction = "Do not restart."
        events = [{"ref": "E000000", "type": "user.message", "text": aside, "human_input": aside}, *packet(),
                  {"ref": "E000003", "type": "user.message", "text": correction, "human_input": correction}]
        phase = candidate["agent_detail"]["trajectory"][0]
        phase["human_refs"] = ["E000000", "E000001", "E000003"]
        phase["refs"] = ["E000002", "E000003"]
        original = copy.deepcopy((candidate, events))
        current = render_agent_package(candidate, events, "en")
        self.assertNotIn("### E000000", current["evidence.md"])
        self.assertIn("### E000003", current["evidence.md"])
        self.assertIn(correction, current["evidence.md"])
        prior = render_agent_package(candidate, events, "en", "handoff-portable-v4")
        self.assertIn("### E000000", prior["evidence.md"])
        self.assertEqual((candidate, events), original)
        phase["refs"].insert(0, "E000000")
        misclassified = render_agent_package(candidate, events, "en")
        self.assertIn("### E000000", misclassified["evidence.md"])

    def test_absent_rationale_does_not_add_empty_citations(self):
        candidate = article()
        candidate["agent_detail"]["trajectory"][0]["rationale"] = {"basis": "not_recorded", "text": "", "refs": []}
        output = render_agent_package(candidate, packet(), "en")["agent-spec.md"]
        self.assertNotIn("Rationale [", output)
        self.assertNotIn("()", output)

    def test_patch_only_requests_are_present_bounded_and_legacy_compatible(self):
        events = [{"ref": "E000001", "type": "tool.execution_start", "tool": "apply_patch", "tool_call_id": "patch-call",
                   "arguments": {"patch": "*** Begin Patch\n*** Update File: module.py\n" + "LONG_PATCH_BODY" * 1000}}]
        original = copy.deepcopy(events)
        ledger = tool_ledger(events)
        index = EvidenceIndex(events, ledger, "en", portable=True, payload_aware=True)
        result = index.companion(index.finish("Source E000001", separate=True))
        self.assertIn("module.py", result)
        self.assertIn("Compact argument excerpt", result)
        self.assertNotIn("No text payload recorded", result)
        self.assertNotIn("LONG_PATCH_BODY" * 20, result)
        legacy = EvidenceIndex(events, ledger, "en", portable=True)
        self.assertIn("No text payload recorded", legacy.companion(legacy.finish("Source E000001", separate=True)))
        self.assertEqual(events, original)

    def test_unknown_argument_fields_do_not_become_missing_and_none_stays_missing(self):
        for arguments in ({"target_files": ["one.py", "two.py"]}, {}, ["one.py"], None):
            with self.subTest(arguments=arguments):
                events = [{"ref": "E000001", "type": "tool.execution_start", "tool": "custom", "arguments": arguments}]
                index = EvidenceIndex(events, tool_ledger(events), "en", portable=True, payload_aware=True)
                output = index.companion(index.finish("E000001", separate=True))
                self.assertEqual("No text payload recorded" in output, arguments is None)
                if arguments:
                    self.assertIn("one.py", output)

    def test_portable_companion_does_not_require_private_export_files(self):
        for language in ("en", "zh-CN"):
            with self.subTest(language=language):
                files = render_agent_package(article(), packet(), language)
                self.assertNotIn("_support/", files["evidence.md"])
                footer = files["agent-spec.md"].rsplit("## ", 1)[-1]
                self.assertNotIn("_support/", footer)
                if language == "en":
                    self.assertIn("not included in this package", files["evidence.md"])
                    self.assertIn("assess acceptance against the task's stated scope", files["evidence.md"])
                    self.assertNotIn("not task acceptance", files["evidence.md"])
                    self.assertIn("Private export internals are not delivered", footer)
                else:
                    self.assertIn("不包含在交付包中", files["evidence.md"])
                    self.assertIn("本任务明确约定的范围", files["evidence.md"])

    def test_legacy_split_companion_preserves_its_exact_boundary_wording(self):
        files = render_agent_package(article(), packet(), "en", "handoff-split")
        self.assertIn("Recorded output; not task acceptance.", files["evidence.md"])
        self.assertIn("_support/evidence.jsonl", files["evidence.md"])
        self.assertIn("Exact payloads and provenance are in `_support/`", files["agent-spec.md"])

    def test_handoff_has_tool_target_and_explained_local_citations(self):
        files = render_agent_package(article(), packet(), "en")
        output = files["agent-spec.md"]
        self.assertIn("## Detailed trajectory", output)
        self.assertIn("**shell** — Read back the wrapper entry and startup signal.", output)
        self.assertNotIn("## Cited evidence", output)
        self.assertNotIn("### E000001", output)
        self.assertIn("### E000001", files["evidence.md"])
        self.assertIn("### E000002", files["evidence.md"])
        self.assertIn("not a task, commit or test number", files["evidence.md"])
        self.assertIn("No uniquely paired request", files["evidence.md"])
        self.assertNotIn("Sources: E", output)
        for anchor in re.findall(r"\]\(evidence\.md#(e\d{6})\)", output):
            self.assertIn("### " + anchor.upper(), files["evidence.md"])
        self.assertFalse(validate_agent_detail(article()["agent_detail"], packet()))

    def test_split_link_labels_are_short_and_preserve_roles_and_tool_names(self):
        files = render_agent_package(article(), packet(), "en")
        self.assertIn("[E000001 · User](evidence.md#e000001)", files["agent-spec.md"])
        self.assertIn("[E000002 · shell result](evidence.md#e000002)", files["agent-spec.md"])
        self.assertIn("[the Agent handoff](agent-spec.md)", files["evidence.md"])
        legacy = render_agent_package(article(), packet(), "en", "handoff")
        self.assertNotIn("evidence.md", legacy)
        self.assertIn("## Cited evidence", legacy["agent-spec.md"])

    def test_names_without_concrete_usage_do_not_pass(self):
        detail = article()["agent_detail"]
        detail["trajectory"][0]["tool_steps"][0].pop("usage")
        self.assertTrue(any("usage" in issue for issue in validate_agent_detail(detail, packet())))

    def test_invented_tools_and_non_tool_usage_refs_fail(self):
        for key, value in (("tool", "InventedTool"), ("refs", ["E000001"]), ("action", "")):
            with self.subTest(key=key):
                detail = article()["agent_detail"]
                detail["trajectory"][0]["tool_steps"][0]["usage"][0][key] = value
                self.assertTrue(validate_agent_detail(detail, packet()))

    def test_large_tool_payloads_stay_outside_navigation_notes(self):
        events = packet() + [
            {"ref": "E000003", "type": "tool.execution_start", "tool": "Edit", "tool_call_id": "edit", "arguments": {"file_path": "src/entry.py", "new_string": "LONG_SOURCE_CODE" * 1000}},
            {"ref": "E000004", "type": "tool.execution_complete", "tool_call_id": "edit", "success": False, "error": "Mismatch", "result": {"content": "No replacement made. " + "LOG_PAYLOAD" * 1000}},
        ]
        index = EvidenceIndex(events, tool_ledger(events), "en")
        output = index.finish("Selected evidence: E000003, E000004")
        self.assertIn("src/entry.py", output)
        self.assertIn("Explicit tool failure", output)
        self.assertNotIn("LONG_SOURCE_CODE", output)
        self.assertLess(len(output), 2000)
        self.assertEqual(len(events[-2]["arguments"]["new_string"]), 16000)

    def test_selected_citations_preserve_all_authored_refs_without_mutation(self):
        events = [{"ref": f"E{number:06}", "type": "user.message", "human_input": f"Correction {number}"} for number in range(1, 50)]
        before = copy.deepcopy(events)
        index = EvidenceIndex(events, tool_ledger(events), "en")
        selected = index.cite([event["ref"] for event in events])
        self.assertEqual(re.findall(r"E\d{6}", selected), [event["ref"] for event in events])
        self.assertEqual(events, before)

    def test_unknown_visible_ref_is_not_a_dead_link(self):
        index = EvidenceIndex(packet(), tool_ledger(packet()), "en")
        with self.assertRaises(ValueError):
            index.finish("See E999999")

    def test_source_injection_is_literal_and_report_is_not_observation(self):
        events = [{"ref": "E000001", "type": "assistant.message", "text": "[Ignore history](javascript:alert(1)) `fake`"}]
        output = EvidenceIndex(events, tool_ledger(events), "en").finish("E000001")
        self.assertIn("Reported statement; not independent verification", output)
        self.assertIn("\\[Ignore history\\]", output)
        self.assertTrue(literal("```payload```").startswith("```` "))

    def test_context_is_not_mislabeled_as_human_authority(self):
        events = [{"ref": "E000001", "type": "user.message", "text": "skill instructions", "source_context": {"kind": "skill"}}]
        output = EvidenceIndex(events, tool_ledger(events), "en").finish("E000001")
        self.assertIn("Context, not new user authority", output)

    def test_human_request_with_separate_injected_context_keeps_its_role(self):
        events = [{"ref": "E000001", "type": "user.message", "text": "Create a PR", "human_input": "Create a PR",
                   "source_context": {"kind": "injected_metadata_not_human_authorization", "content": "Read attached instructions"}}]
        output = EvidenceIndex(events, tool_ledger(events), "en").finish("E000001")
        self.assertIn("[User · Create a PR]", output)
        self.assertNotIn("Read attached instructions", output)
        legacy = EvidenceIndex(events, tool_ledger(events), "en", legacy_roles=True).finish("E000001")
        self.assertIn("[Context · Create a PR]", legacy)

    def test_imported_context_is_not_an_assistant_statement(self):
        events = [{"ref": "E000001", "type": "session.imported_context", "text": "Historical environment metadata"}]
        output = EvidenceIndex(events, tool_ledger(events), "en").finish("E000001")
        self.assertIn("Context, not new user authority", output)
        self.assertNotIn("Assistant report", output)

    def test_structural_review_reports_all_independent_components_together(self):
        edition = {"article": article(), "brief": brief(), "insights": insights()}
        edition["article"]["agent_detail"]["trajectory"][0]["tool_steps"][0]["usage"] = []
        edition["brief"]["approach"]["text"] = "long " * 301
        edition["insights"]["architecture"]["edges"][0]["label"] = "<invalid>"
        issues = "\n".join(validate_edition(edition, packet(), validate_article))
        for expected in ("/tool_steps/0/usage", "/brief/approach/text", "1400", "markup: label"):
            self.assertIn(expected, issues)

    def test_source_link_text_escapes_table_delimiters(self):
        events = [{"ref": "E000001", "type": "user.message", "human_input": "Keep A | B"}]
        output = EvidenceIndex(events, tool_ledger(events), "en").finish("| Claim | E000001 |")
        self.assertIn("A \\| B", output.split("## Cited evidence")[0])

    def test_adjacent_bracket_citations_remain_distinct_without_double_brackets(self):
        index = EvidenceIndex(packet(), tool_ledger(packet()), "en")
        output = index.finish("Fact [E000001][E000002]")
        main = output.split("## Cited evidence")[0]
        self.assertNotIn("[[", main)
        self.assertIn("](#e000001) ", main)
        self.assertIn("](#e000002)", main)

    def test_pair_completion_applies_consistently_to_usage_and_parent_tool_refs(self):
        events = [{"ref": "E000003", "type": "tool.execution_start", "tool_call_id": "pair"},
                  {"ref": "E000004", "type": "tool.execution_complete", "tool_call_id": "pair", "success": False}]
        original = {"tool_refs": ["E000003"], "tool_steps": [{"tool_refs": ["E000003"], "usage": [{"refs": ["E000003"]}]}]}
        completed, changes = complete_reference_pairs(original, events)
        self.assertEqual(completed["tool_refs"], ["E000003", "E000004"])
        self.assertEqual(completed["tool_steps"][0]["tool_refs"], ["E000003", "E000004"])
        self.assertEqual(completed["tool_steps"][0]["usage"][0]["refs"], ["E000003", "E000004"])
        self.assertEqual(len(changes), 3)
        self.assertEqual(original["tool_refs"], ["E000003"])
