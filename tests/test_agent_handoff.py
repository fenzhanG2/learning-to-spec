import copy
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from session_spec.agent_handoff import render_agent, tool_ledger, validate_agent_detail
from session_spec.agent_transfer import fenced, inline_tool_plan
from session_spec.backend import ModelResponseError
from session_spec.language import language_contract, resolve_language, validate_language
from session_spec.model_io import generate_json
from session_spec.prompts import extraction_prompt, repair_patch_prompt, repair_prompt, synthesis_prompt
from session_spec.story_draft import generate_draft, normalize_containers
from session_spec.story_grounding import validate_grounding
from session_spec.story_editor import PROMPTS, generate_edition
from session_spec.story_article import validate_article
from test_story_pipeline import FakeBackend, article as fixture_article, brief, edition_review, insights, packet


def article():
    return fixture_article(schema="agent-detail/v2")


class AgentHandoffTests(unittest.TestCase):
    def test_absent_rationale_does_not_require_invented_placeholder_prose(self):
        candidate = fixture_article(schema="agent-detail/v3")
        detail = candidate["agent_detail"]
        detail["trajectory"][0]["rationale"] = {"basis": "not_recorded", "text": "", "refs": []}
        self.assertEqual(validate_agent_detail(detail, packet()), [])
        detail["trajectory"][0]["rationale"]["refs"] = ["E000001"]
        self.assertTrue(any("not_recorded must have refs=[]" in issue for issue in validate_agent_detail(detail, packet())))

    def test_missing_prose_citations_report_the_specific_field_to_repair(self):
        candidate = fixture_article(schema="agent-detail/v3")
        candidate["agent_markdown"] = "# Work\n\n## Contract\nAn uncited assertion."
        self.assertTrue(any("/article/agent_markdown" in issue for issue in validate_article(candidate, packet())))

    def test_auto_language_preserves_source_without_a_target(self):
        contract = language_contract()
        self.assertIn("SOURCE_LANGUAGE_POLICY", contract)
        self.assertIn("No target language is prescribed", contract)
        self.assertNotIn("The output language is", contract)
        self.assertEqual(resolve_language(packet())["requested"], "auto")
        self.assertEqual(validate_language({"article": article()}, "auto"), [])

    def test_auto_checks_source_prose_without_prescribing_a_language_tag(self):
        events = [{"human_input": "Keep the retry count bounded and preserve the failed test. " * 8}]
        candidate = {"article": {"agent_markdown": "这是一份错误地跟随导出提示语言的正文。" * 15}}
        issues = validate_language(candidate, "auto", events)
        self.assertTrue(issues)
        self.assertIn("/article/agent_markdown", issues[0])
        self.assertNotIn("English output requested", issues[0])
        self.assertEqual(validate_language(candidate, "zh-CN", events), [])
        self.assertEqual(validate_language(candidate, "auto", [{"human_input": "保留关键细节。" + events[0]["human_input"]}]), [])
        candidate["article"]["agent_markdown"] = "Source-faithful narrative.\n```text\n" + "示例代码内容。" * 30 + "\n```"
        self.assertEqual(validate_language(candidate, "auto", events), [])

    def test_canonical_auto_prompts_do_not_prescribe_a_language(self):
        prompts = [extraction_prompt({}, "auto"), synthesis_prompt([], [], {"id": "sample"}, "auto"),
                   repair_prompt({}, [], [], [], "auto"), repair_patch_prompt({}, [], [], [], "auto")]
        for prompt in prompts:
            self.assertIn("SOURCE_LANGUAGE_POLICY", prompt)
            self.assertNotIn("The output language is", prompt)
            self.assertNotIn("Language: auto", prompt)

    def test_short_route_labels_cannot_follow_the_exporter_language(self):
        events = [{"human_input": "Keep the retry count bounded and preserve the failed test. " * 8}]
        candidate = {"article": {"title": "Bounded retry", "route": [
            {"title": "读现有循环", "detail": "发现 while True 无上限"},
            {"title": "加尝试上限", "detail": "改为 range(3)，保留可注入 sleep"}]}}
        issues = validate_language(candidate, 'auto', events)
        self.assertTrue(issues)
        self.assertIn('/article/route/0/title', issues[0])
        self.assertIn('/article/route/1/detail', issues[0])
        self.assertEqual(validate_language(candidate, 'zh-CN', events), [])
        self.assertEqual(validate_language(candidate, 'auto', [{'human_input': '保留源语言。' + events[0]['human_input']}]), [])

    def test_route_keeps_literal_source_labels_and_code(self):
        events = [{'human_input': 'Document the existing status labels without translating source strings. ' * 8},
                  {'text': 'The exact UI label is 恢复工作 and the function name is 重试请求.'}]
        candidate = {'article': {'route': [{'title': '恢复工作', 'detail': 'Inspect `重试请求` before changing behavior.'}]}}
        self.assertEqual(validate_language(candidate, 'auto', events), [])

    def test_default_draft_is_not_an_explicit_language_request(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            draft = {"article": article(), "brief": brief(), "insights": insights()}
            backend = FakeBackend([draft, draft])
            generate_draft(directory, packet(), None, backend)
            generate_draft(directory, packet(), None, backend)
            self.assertEqual(len(backend.calls), 1)
            self.assertIn("SOURCE_LANGUAGE_POLICY", backend.prompts[0])
            self.assertNotIn("The output language is", backend.prompts[0])
            generate_draft(directory, packet(), None, backend, language="en")
            self.assertEqual(len(backend.calls), 2)
            self.assertIn("The output language is en", backend.prompts[1])

    def test_default_reviewer_does_not_prescribe_a_language(self):
        with tempfile.TemporaryDirectory() as temporary:
            draft = {"article": article(), "brief": brief(), "insights": insights()}
            backend = FakeBackend([edition_review()])
            self.assertEqual(generate_edition(Path(temporary), draft, packet(), backend, validate_article), draft)
            self.assertIn("SOURCE_LANGUAGE_POLICY", backend.prompts[0])
            self.assertNotIn("The output language is", backend.prompts[0])

    def test_language_change_invalidates_initial_draft_cache(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            draft = {"article": article(), "brief": brief(), "insights": insights()}
            backend = FakeBackend([draft, draft])
            generate_draft(directory, packet(), None, backend, language="en")
            generate_draft(directory, packet(), None, backend, language="en")
            self.assertEqual(len(backend.calls), 1)
            generate_draft(directory, packet(), None, backend, language="zh-CN")
            self.assertEqual(len(backend.calls), 2)
            self.assertIn("output language is en", backend.prompts[0])

    def test_changed_review_contract_rechecks_old_findings_instead_of_applying_them(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prompts = root / "prompts"
            shutil.copytree(PROMPTS, prompts)
            output = root / "work"
            output.mkdir()
            draft = {"article": article(), "brief": brief(), "insights": insights()}
            with patch("session_spec.story_editor.PROMPTS", prompts):
                with self.assertRaises(ValueError):
                    generate_edition(output, draft, packet(), FakeBackend([edition_review([{"reason": "Recheck this old finding"}])]), validate_article, max_repairs=0)
                method = prompts / "story-editor-method.md"
                method.write_text(method.read_text(encoding="utf-8") + "\nNew rendering rule.\n", encoding="utf-8")
                backend = FakeBackend([edition_review()])
                self.assertEqual(generate_edition(output, draft, packet(), backend, validate_article, max_repairs=0), draft)
                self.assertIn("Recheck this old finding", backend.prompts[0])
                self.assertEqual(len(backend.calls), 1)

    def test_internal_role_translation_is_not_a_valid_blocker(self):
        edition = {"article": article(), "brief": brief(), "insights": insights()}
        review = {"issues": [{"path": "/insights/architecture/nodes/0/role", "quote": "控制", "severity": "major",
                              "kind": "contract", "contract_quote": "Write English", "evidence": [], "reason": "Translate the role"}]}
        errors = validate_grounding(review, edition, packet(), "Write English")
        self.assertTrue(any("internal enum" in error for error in errors))

    def test_container_relocation_preserves_every_value_and_original(self):
        nested = {"article": {**article(), "brief": brief(), "insights": insights()}}
        before = copy.deepcopy(nested)
        normalized, changes = normalize_containers(nested)
        self.assertEqual(normalized, {"article": article(), "brief": brief(), "insights": insights()})
        self.assertEqual(nested, before)
        self.assertTrue(changes)
        ambiguous = {**nested, "brief": {"conflict": True}}
        self.assertEqual(normalize_containers(ambiguous), (ambiguous, []))

    def test_source_language_ignores_tools_and_code(self):
        events = [{"human_input": "Please fix the timeout. ```\n中文中文中文\n```"}, {"type": "tool.execution_complete", "text": "中文" * 300}]
        self.assertEqual(resolve_language(events)["language"], "en")
        self.assertEqual(resolve_language(packet())["language"], "zh-CN")
        self.assertEqual(resolve_language(events, "zh-CN")["method"], "explicit")
        self.assertEqual(resolve_language([{"human_input": "この問題を修正してください。"}])["language"], "ja")
        with self.assertRaises(ValueError):
            resolve_language(events, 'en\" onclick="bad')

    def test_chinese_output_is_not_accepted_as_english(self):
        edition = {"article": article(), "brief": brief(), "insights": insights()}
        edition["article"]["chapters"][0]["markdown"] = "这是中文段落。" * 20
        self.assertTrue(validate_language(edition, "en"))
        self.assertFalse(validate_language(edition, "zh-CN"))

    def test_trajectory_covers_short_user_corrections(self):
        detail = article()["agent_detail"]
        self.assertFalse(validate_agent_detail(detail, packet()))
        events = packet() + [{"ref": "E000003", "human_input": "No, keep the timeout.", "type": "user.message"}]
        self.assertIn("E000003", " ".join(validate_agent_detail(detail, events)))
        broken = copy.deepcopy(detail)
        broken["trajectory"][0]["rationale"]["basis"] = "private_thinking"
        self.assertTrue(validate_agent_detail(broken, packet()))

    def test_non_tool_refs_and_invented_paths_fail(self):
        detail = article()["agent_detail"]
        detail["trajectory"][0]["tool_refs"] = ["E000001"]
        detail["paths"][0]["phase_ids"] = ["invented"]
        errors = " ".join(validate_agent_detail(detail, packet()))
        self.assertIn("tool_refs", errors)
        self.assertIn("phase IDs", errors)

    def test_ledger_preserves_payload_and_unknown_success(self):
        events = [{"ref": "E000010", "type": "tool.execution_start", "tool": "Bash", "tool_call_id": "call", "arguments": {"command": "pytest -q", "extra": "x" * 1000}},
                  {"ref": "E000011", "type": "tool.execution_complete", "tool_call_id": "call", "result": {"content": "5 passed"}},
                  {"ref": "E000012", "type": "tool.execution_complete", "tool_call_id": "orphan", "success": False, "error": "denied"}]
        ledger = tool_ledger(events)
        self.assertEqual(ledger["calls"][0]["arguments"], events[0]["arguments"])
        self.assertEqual(ledger["calls"][0]["status"], "result_recorded")
        self.assertEqual(ledger["unpaired_results"], [events[2]])
        ambiguous = tool_ledger([events[0], {**events[0], "ref": "E000013"}, events[1]])
        self.assertTrue(all(not call["results"] for call in ambiguous["calls"]))
        self.assertEqual(len(ambiguous["unpaired_results"]), 1)

    def test_ledger_failure_and_missing_result_are_separate(self):
        events = [{"ref": "E000010", "type": "tool.execution_start", "tool_call_id": "call"},
                  {"ref": "E000011", "type": "tool.execution_complete", "tool_call_id": "call", "success": False},
                  {"ref": "E000012", "type": "tool.execution_start", "tool_call_id": "missing"}]
        self.assertEqual([call["status"] for call in tool_ledger(events)["calls"]], ["explicit_failure", "no_result"])

    def test_render_keeps_working_spec_and_structured_history(self):
        output = render_agent(article(), packet(), "en")
        self.assertTrue(output.startswith(article()["agent_markdown"].splitlines()[0]))
        self.assertIn("## Resume here", output)
        self.assertIn("## Continue this task", output)
        self.assertIn("## Solve a similar task", output)
        self.assertIn("## Decision trajectory", output)
        self.assertIn("## Successful, failed and unresolved paths", output)
        self.assertIn("_support/tool-ledger.json", output)
        self.assertIn("E000002", output)
        legacy = article()
        legacy.pop("agent_detail")
        self.assertEqual(render_agent(legacy, packet()), legacy["agent_markdown"])

    def test_legacy_expanded_tools_include_arguments_results_before_interpretation(self):
        candidate = article()
        events = packet() + [{"ref": "E000003", "type": "tool.execution_start", "tool": "Bash", "tool_call_id": "check", "arguments": {"command": "pytest -q --strict-markers"}},
                             {"ref": "E000004", "type": "tool.execution_complete", "tool_call_id": "check", "result": {"content": "2 failed"}, "success": False}]
        phase = candidate["agent_detail"]["trajectory"][0]
        phase["tool_refs"] += ["E000003", "E000004"]
        phase["tool_steps"].append({"purpose": "Check the new behavior", "tool_refs": ["E000003", "E000004"], "finding": "The check failed, do not proceed", "decision": "Investigate before changing", "refs": ["E000003", "E000004"]})
        output = render_agent(candidate, events, trajectory_style="expanded")
        self.assertNotIn("## Complete tool-use index", output)
        self.assertLess(output.index("pytest -q --strict-markers"), output.index("The check failed, do not proceed"))
        self.assertLess(output.index("2 failed"), output.index("Investigate before changing"))
        self.assertIn("explicit_failure", output)
        self.assertLess(output.index("pytest -q --strict-markers"), output.index("## Successful, failed"))

    def test_legacy_expanded_tool_units_appear_once_near_a_phase(self):
        events = packet() + [{"ref": "E000003", "type": "tool.execution_start", "tool_call_id": "unused", "arguments": {"command": "pwd"}},
                             {"ref": "E000004", "type": "tool.execution_complete", "tool_call_id": "orphan", "result": {"content": "unpaired result"}}]
        plan = inline_tool_plan(article()["agent_detail"], events, tool_ledger(events))
        refs = [unit["source_ref"] for phase in plan for group in [*phase["steps"], phase["remaining"]] for unit in group]
        self.assertCountEqual(refs, ["E000002", "E000003", "E000004"])
        output = render_agent(article(), events, trajectory_style="expanded")
        self.assertIn("no_result", output)
        self.assertIn("unpaired_result", output)
        self.assertIn("unpaired result", output)

    def test_decision_trajectory_keeps_meaning_without_tool_payloads(self):
        candidate = article()
        events = packet() + [
            {"ref": "E000003", "type": "tool.execution_start", "tool": "Bash", "tool_call_id": "check", "arguments": {"command": "pytest -q --strict-markers", "extra": "RAW_ARGUMENT" * 5000}},
            {"ref": "E000004", "type": "tool.execution_complete", "tool_call_id": "check", "success": False, "result": {"content": "RAW_LOG" * 5000}},
            {"ref": "E000005", "type": "tool.execution_start", "tool": "RoutineNavigation", "tool_call_id": "routine", "arguments": {"command": "ROUTINE_PAYLOAD"}},
            {"ref": "E000006", "type": "tool.execution_complete", "tool_call_id": "orphan", "result": {"content": "ORPHAN_PAYLOAD"}},
        ]
        phase = candidate["agent_detail"]["trajectory"][0]
        phase["tool_refs"].extend(["E000003", "E000004"])
        phase["tool_steps"].append({"purpose": "Check strict marker behavior", "tool_refs": ["E000003", "E000004"],
                                    "finding": "`pytest -q --strict-markers` failed on an unknown marker; no feature acceptance.",
                                    "decision": "Fix marker registration before rerunning.", "refs": ["E000003", "E000004"]})
        phase["rationale"] = {"basis": "inferred", "text": "Registration was the discriminating check, not an established root cause.", "refs": ["E000004"]}
        before = copy.deepcopy((candidate, events))
        output = render_agent(candidate, events)
        for value in ("## Decision trajectory", "Tools: Bash", "pytest -q --strict-markers", "failed on an unknown marker",
                      "Fix marker registration", "Rationale [inferred]", "E000004", phase["observation"], phase["next_state"]):
            self.assertIn(value, output)
        for value in ("RAW_ARGUMENT", "RAW_LOG", "ROUTINE_PAYLOAD", "ORPHAN_PAYLOAD", "RoutineNavigation",
                      "Historical tool", "Recorded result:", "Other tool activity", "Complete tool-use index"):
            self.assertNotIn(value, output)
        trajectory = output.split("## Decision trajectory", 1)[1].split("## Successful, failed", 1)[0]
        self.assertEqual(trajectory.count("Tools: Bash"), 1)
        self.assertEqual(trajectory.count("Sources:"), 1)
        self.assertEqual((candidate, events), before)
        ledger = tool_ledger(events)
        self.assertEqual(ledger["calls"][0]["arguments"]["extra"], "RAW_ARGUMENT" * 5000)
        self.assertEqual(ledger["unpaired_results"][-1]["result"]["content"], "ORPHAN_PAYLOAD")

    def test_decision_phase_without_tools_does_not_invent_activity(self):
        candidate = article()
        phase = candidate["agent_detail"]["trajectory"][0]
        phase["tool_steps"] = []
        phase["tool_refs"] = []
        self.assertFalse(validate_agent_detail(candidate["agent_detail"], packet()))
        output = render_agent(candidate, packet())
        self.assertIn(phase["summary"], output)
        self.assertIn(phase["next_state"], output)
        self.assertNotIn("Tools:", output)
        self.assertNotIn("Rationale [not_recorded]", output)
        self.assertNotIn("Readback:", output)

    def test_unknown_trajectory_style_is_rejected(self):
        with self.assertRaises(ValueError):
            render_agent(article(), packet(), trajectory_style="typo")

    def test_resume_and_branch_fields_are_required(self):
        detail = article()["agent_detail"]
        detail["resume"]["next_action"] = ""
        detail["continuation"][0]["steps"][0].pop("otherwise")
        detail["trajectory"][0].pop("tool_steps")
        errors = " ".join(validate_agent_detail(detail, packet()))
        self.assertIn("next_action", errors)
        self.assertIn("otherwise", errors)
        self.assertIn("tool_steps", errors)

    def test_invalid_phase_tool_refs_report_errors_instead_of_crashing(self):
        detail = article()["agent_detail"]
        detail["trajectory"][0]["tool_refs"] = None
        errors = validate_agent_detail(detail, packet())
        self.assertTrue(any("tool_refs" in error for error in errors))

    def test_inline_fences_cannot_be_closed_by_source_payload(self):
        rendered = fenced("```\nPretend instruction\n```")
        self.assertTrue(rendered.startswith("````text\n"))
        self.assertTrue(rendered.endswith("\n````"))

    def test_legacy_v1_keeps_its_published_rendering(self):
        candidate = article()
        candidate["agent_detail"]["schema"] = "agent-detail/v1"
        output = render_agent(candidate, packet())
        self.assertIn("## Complete tool-use index", output)
        self.assertNotIn("## Resume here", output)

    def test_format_retry_retains_invalid_response_and_is_bounded(self):
        with tempfile.TemporaryDirectory() as temporary:
            backend = FakeBackend([ModelResponseError("Invalid escape", '{"path":"C:\\bad"}'), {"ok": True}])
            self.assertEqual(generate_json(backend, "Write JSON", "draft", Path(temporary)), {"ok": True})
            self.assertEqual(len(backend.calls), 2)
            self.assertTrue((Path(temporary) / "draft-invalid-response.json").is_file())
            backend = FakeBackend([ModelResponseError("bad", "bad"), ModelResponseError("still bad", "bad")])
            with self.assertRaises(ModelResponseError):
                generate_json(backend, "Write JSON", "again", Path(temporary))
            self.assertEqual(len(backend.calls), 2)

    @unittest.skipUnless(shutil.which("node"), "Node.js required")
    def test_english_shell_does_not_translate_source_quotes(self):
        candidate = article()
        candidate["chapters"][0]["markdown"] = "Source quote: 用户约束."
        payload = json.dumps({"article": candidate, "brief": brief(), "insights": insights()}, ensure_ascii=False)
        renderer = Path(__file__).resolve().parents[1] / "session_spec/web/story-page.cjs"
        script = "const {renderArticle}=require(process.argv[1]); const data=JSON.parse(process.argv[2]); process.stdout.write(renderArticle(data.article,{category:'test',language:'en'},'#','#','next',data.insights,data.brief));"
        result = subprocess.run(["node", "-e", script, str(renderer), payload], capture_output=True, text=True, encoding="utf-8", check=True)
        self.assertIn('<html lang="en">', result.stdout)
        self.assertIn('Agent handoff · Markdown source', result.stdout)
        self.assertIn('Source quote: 用户约束.', result.stdout)
        self.assertNotIn('aria-label="故事路线"', result.stdout)
        self.assertIn('Artifact architecture', result.stdout)
        self.assertIn('>Control</text>', result.stdout)
