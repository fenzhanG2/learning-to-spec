import copy
import contextlib
import hashlib
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from session_spec.pipeline import write_json
from session_spec.cli import main as cli_main
from session_spec.story_context import file_activity, root_packet, select_story_windows, tool_exchanges
from session_spec.story_brief import PROMPTS, generate_brief, validate_brief
from session_spec.story_editor import CHECKS, compact_evidence, generate_edition, validate_edition, validate_feedback, validate_review
from session_spec.story_insights import generate_insights, validate_insights
from session_spec.story_grounding import complete_reference_pairs, resolve_review_locations, validate_grounding
from session_spec.story_pipeline import reviewed_article, run_story, validate_article, validate_story
from session_spec.agent_handoff import render_agent
from session_spec.transfer_probe import CHECKS as PROBE_CHECKS


def packet():
    return [
        {"ref": "E000001", "origin": "root", "type": "user.message", "turn": 1, "text": "保留功能，只隐藏窗口。", "human_input": "保留功能，只隐藏窗口。"},
        {"ref": "E000002", "origin": "root", "type": "tool.execution_complete", "turn": 1, "tool": "shell", "result": {"content": "Readback: task calls wrapper; startup signal observed."}, "success": True},
    ]


def article(schema="agent-detail/v3"):
    value = {
        "title": "窗口消失以后", "subtitle": "留下入口与边界", "period": "2026-01-01",
        "opening": "用户希望窗口不再出现，但原来的功能要保留。", "outcome": "启动入口已改变；没有做重启测试。",
        "route": [{"title": "调整入口", "detail": "保留原有功能"}],
        "chapters": [{"id": "mechanism", "title": "保留运行关系", "markdown": "任务调用包装器。", "refs": ["E000001", "E000002"], "details": []}],
        "checks": [{"question": "能启动吗", "observed": "看到启动信号", "limit": "不证明完整功能", "refs": ["E000002"]}],
        "reader_coverage": [{"question": "如何工作", "chapters": ["mechanism"], "refs": ["E000002"]}],
        "human_input_coverage": [{"ref": "E000001", "treatment": "mechanism 保留功能"}],
        "agent_markdown": "# 交接\n\n入口已改变。需要重启时先确认授权。来源 E000001、E000002；证据在 _support/evidence.jsonl。\n",
        "agent_detail": {"schema": schema,
            "resume": {"checkpoint": "入口已改变，重启行为未验收。", "workspace": "记录只包含任务与包装器，当前状态需读回。", "next_action": "若继续验证，先读回任务入口。", "verification_boundary": "启动信号不是长期功能验收。", "refs": ["E000001", "E000002"]},
            "continuation": [{"title": "复核入口", "basis": "proposed", "trigger": "用户希望继续验证。", "steps": [{"kind": "inspect", "action": "读回任务入口与包装器。", "precondition": "能访问当前配置。", "expected": "仍调用包装器，再决定验证方式。", "otherwise": "先解释配置差异，不盲目重启。", "refs": ["E000002"]}], "done_when": "实际启动场景符合保留功能的要求。", "stop_when": "缺少验证权限或配置与记录不符。", "refs": ["E000001", "E000002"]}],
            "recipes": [{"title": "区分入口与功能", "basis": "proposed", "when": "需要隐藏启动窗口但保留原功能。", "adapt": "替换任务名称并核对调用链。", "procedure": ["确认入口、包装器和被调用功能。", "按真实启动场景验证功能而非只看进程。"], "avoid": "不能把启动信号当完整功能验收。", "verify": "在适用启动场景验证功能。", "refs": ["E000001", "E000002"]}],
            "trajectory": [{"id": "entry-change", "title": "调整入口",
            "tool_steps": [{"purpose": "确认入口。", "tool_refs": ["E000002"], "finding": "读回入口与启动信号。", "decision": "完整功能仍未验收。", "refs": ["E000002"]}],
            "human_refs": ["E000001"], "summary": "保留功能，改变入口。", "rationale": {"basis": "not_recorded", "text": "没有记录选择包装器的理由。", "refs": []},
            "tool_refs": ["E000002"], "observation": "读回入口和启动信号。", "outcome": "partial", "next_state": "重启行为未测试。", "refs": ["E000001", "E000002"]}],
            "paths": [{"title": "隐藏启动", "outcome": "partial", "phase_ids": ["entry-change"], "reason": "启动信号不是完整功能验收。", "reuse_condition": "需要重启时先确认授权。", "refs": ["E000002"]}]},
    }
    if schema == "agent-detail/v3":
        value["agent_detail"]["trajectory"][0]["tool_steps"][0]["usage"] = [{"tool": "shell", "action": "Read back the wrapper entry and startup signal.", "refs": ["E000002"]}]
    return value


def insights(include=True):
    architecture = {"decision": "omit", "reason": "只有需求，还没有实际实现信息。"}
    if include:
        architecture = {
            "decision": "include", "reason": "有读回结果", "kind": "final_artifact", "after_chapter": "mechanism",
            "title": "实际启动链", "scope": "会话最后的入口，不是未来设计。",
            "nodes": [{"id": "task", "title": "任务", "role": "控制", "detail": "管理入口", "refs": ["E000002"]},
                      {"id": "wrapper", "title": "包装器", "role": "处理", "detail": "隐藏启动", "refs": ["E000002"]}],
            "edges": [{"from": "task", "to": "wrapper", "label": "调用", "kind": "flow", "basis": "implementation", "refs": ["E000002"]}],
            "evidence_summary": "配置读回支持这条调用关系。", "limits": "没有做重启测试。",
        }
    return {"schema": "story-insights/v1", "architecture": architecture, "closing": {
        "title": "安静不等于停止", "paragraphs": ["这次保留了功能，只改变入口。", "有明确调用链时才适用这个判断，不等于长期行为已经验收。"],
        "anchors": [{"chapter": "mechanism", "turning_point": "用户要求保留功能", "refs": ["E000001"]}],
        "principle": "拆开入口与功能", "applicability": "有调用链证据", "non_claim": "不代表通过长期测试",
    }}


def brief():
    return {
        "schema": "story-brief/v1",
        "background": {"text": "原有任务仍需要运行。", "refs": ["E000001"]},
        "problem": {"text": "用户不想看到窗口。", "refs": ["E000001"]},
        "goals": [{"text": "隐藏窗口而不损失原功能。", "refs": ["E000001"]}],
        "approach": {"text": "在启动入口加一层包装。", "refs": ["E000002"]},
        "non_goals": [],
        "scope": [{"text": "这次修改启动入口。", "refs": ["E000002"]}],
        "constraints": [{"kind": "requirement", "text": "保留原有功能。", "refs": ["E000001"]}],
        "status": {"text": "启动信号已观察到，不代表完整功能已经验证。", "refs": ["E000002"]},
    }


def edition_review(issues=None):
    findings = copy.deepcopy(issues or [])
    for issue in findings:
        issue.setdefault("path", "/article/agent_markdown")
        issue.setdefault("quote", "交接")
        issue.setdefault("severity", "major")
        issue.setdefault("kind", "source_fact")
        issue.setdefault("evidence", [{"ref": "E000001", "quote": "保留功能", "origin": "human"}])
    return {"issues": findings, "suggestions": [], "checked": [{"category": category, "note": "Checked the document and its source."} for category in CHECKS], "summary": "Reviewed"}


class FakeBackend:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.prompts = []

    def generate(self, prompt, label):
        self.calls.append({"label": label, "tool_calls": 0})
        self.prompts.append(prompt)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return copy.deepcopy(response)


def transfer_review(findings=None):
    return {"schema": "transfer-probe/v1", "findings": findings or [],
            "checked": [{"category": category, "note": "Checked the supplied pair, without executing it."} for category in PROBE_CHECKS],
            "summary": "Scripted reader response for offline pipeline tests."}


class StorySchemaTests(unittest.TestCase):
    def test_human_citation_diagnostic_identifies_field_and_preserves_agent_refs(self):
        candidate = article()
        candidate["checks"][0]["observed"] += " (E000002)"
        candidate["chapters"][0]["details"] = [{"title": "Details E000002", "markdown": "Known mechanism"}]
        issues = validate_article(candidate, packet())
        self.assertEqual(len(issues), 2)
        self.assertTrue(any("/article/checks/0/observed" in issue for issue in issues))
        self.assertTrue(any("/article/chapters/0/details/0/title" in issue for issue in issues))
        self.assertTrue(all("Do not remove the required inline citations" in issue for issue in issues))
        candidate["checks"][0]["observed"] = "Observed startup signal."
        candidate["chapters"][0]["details"][0]["title"] = "Details"
        self.assertEqual(validate_article(candidate, packet()), [])

    def test_quote_locations_recover_unique_matches_without_changing_roles(self):
        draft = {"article": article(), "insights": insights(), "brief": brief()}
        review = edition_review([{"path": "/article/title", "quote": "入口已改变。", "evidence": [{"ref": "E000099", "origin": "human", "quote": "保留功能"}]}])
        resolved, changes = resolve_review_locations(review, draft, packet())
        self.assertEqual(resolved["issues"][0]["path"], "/article/agent_markdown")
        self.assertEqual(resolved["issues"][0]["evidence"][0]["ref"], "E000001")
        self.assertEqual(len(changes), 2)
        self.assertEqual(validate_grounding(resolved, draft, packet(), ""), [])
        evidence = packet() + [{"ref": "E000003", "type": "assistant.message", "text": "Restore or save?"}]
        review["issues"][0]["evidence"] = [{"ref": "E000003", "origin": "human", "quote": "Restore or save?"}]
        resolved, _ = resolve_review_locations(review, draft, evidence)
        self.assertTrue(validate_grounding(resolved, draft, evidence, ""))

    def test_quote_relocation_rejects_ambiguous_matches(self):
        draft = {"article": article(), "insights": insights(), "brief": brief()}
        evidence = packet() + [{"ref": "E000003", "type": "user.message", "human_input": "保留功能"}]
        review = edition_review([{"evidence": [{"ref": "E000099", "origin": "human", "quote": "保留功能"}]}])
        resolved, changes = resolve_review_locations(review, draft, evidence)
        self.assertEqual(changes, [])
        self.assertTrue(validate_grounding(resolved, draft, evidence, ""))

    def test_read_display_prefix_matching_does_not_collapse_code_whitespace(self):
        draft = {"article": article(), "insights": insights(), "brief": brief()}
        evidence = packet() + [{"ref": "E000003", "type": "tool.execution_complete", "tool": "Read",
                                "result": {"content": '  41→const value = "two  spaces"\n  42→return value'}}]
        review = edition_review([{"evidence": [{"ref": "E000003", "origin": "tool", "quote": 'const value = "two  spaces"\nreturn value'}]}])
        self.assertEqual(validate_grounding(review, draft, evidence, ""), [])
        review["issues"][0]["evidence"][0]["quote"] = 'const value = "two spaces"\nreturn value'
        self.assertTrue(validate_grounding(review, draft, evidence, ""))

    def test_source_grounding_does_not_strip_real_arrows_from_string_operands(self):
        draft = {"article": article(), "insights": insights(), "brief": brief()}
        text = 'assert output == """\n1→alpha\n2→beta\n"""'
        evidence = packet() + [{"ref": "E000003", "type": "tool.execution_complete", "tool": "Read", "result": {"content": text}}]
        review = edition_review([{"evidence": [{"ref": "E000003", "origin": "tool", "quote": text}]}])
        self.assertEqual(validate_grounding(review, draft, evidence, ""), [])
        review["issues"][0]["evidence"][0]["quote"] = 'assert output == """\nalpha\nbeta\n"""'
        self.assertTrue(validate_grounding(review, draft, evidence, ""))

    def test_agent_cannot_offer_refs_without_providing_any(self):
        candidate = article()
        candidate["agent_markdown"] = "# Handoff\nConsult refs in the supporting evidence."
        self.assertTrue(validate_article(candidate, packet()))

    def test_assistant_options_cannot_ground_a_claim_of_human_choice(self):
        draft = {"article": article(), "insights": insights(), "brief": brief()}
        evidence = packet() + [{"ref": "E000003", "type": "assistant.message", "text": "Choose restore or auto-save?"}]
        review = edition_review([{"kind": "human_requirement", "evidence": [{"ref": "E000003", "origin": "human", "quote": "Choose restore"}]}])
        self.assertTrue(validate_grounding(review, draft, evidence, ""))
        review["issues"][0]["evidence"] = [{"ref": "E000001", "origin": "human", "quote": "保留功能"}]
        self.assertEqual(validate_grounding(review, draft, evidence, ""), [])

    def test_review_needs_real_manuscript_text_and_contract_not_invented_rules(self):
        draft = {"article": article(), "insights": insights(), "brief": brief()}
        review = edition_review([{"kind": "contract", "contract_quote": "An invented publishing rule", "evidence": []}])
        self.assertTrue(validate_grounding(review, draft, packet(), "Real publishing rule"))
        review["issues"][0]["contract_quote"] = "Real publishing rule"
        self.assertEqual(validate_grounding(review, draft, packet(), "Real publishing rule"), [])
        review["issues"][0]["quote"] = "This sentence does not exist"
        self.assertTrue(validate_grounding(review, draft, packet(), "Real publishing rule"))

    def test_ungrounded_criticism_retries_the_review_without_rewriting_the_draft(self):
        draft = {"article": article(), "insights": insights(), "brief": brief()}
        unsupported = edition_review([{"quote": "A sentence absent from the actual draft", "reason": "Unsupported criticism"}])
        with tempfile.TemporaryDirectory() as temporary:
            backend = FakeBackend([unsupported, edition_review()])
            actual = generate_edition(Path(temporary), draft, packet(), backend, validate_article)
            self.assertEqual(actual, draft)
            self.assertEqual([call["label"] for call in backend.calls],
                             ["story-edition-review", "story-edition-review-grounding-retry"])
        with tempfile.TemporaryDirectory() as temporary:
            backend = FakeBackend([unsupported, unsupported])
            with self.assertRaises(ValueError):
                generate_edition(Path(temporary), draft, packet(), backend, validate_article)
            self.assertFalse((Path(temporary) / "edition.json").exists())
            self.assertEqual(len(backend.calls), 2)

    def test_reference_completion_preserves_failure_and_does_not_cross_calls(self):
        events = [{"ref": "E000001", "type": "tool.execution_start", "tool_call_id": "call-1"},
                  {"ref": "E000002", "type": "tool.execution_complete", "tool_call_id": "call-1", "success": False},
                  {"ref": "E000003", "type": "tool.execution_complete", "tool_call_id": "call-2"}]
        original = {"refs": ["E000001"]}
        completed, changes = complete_reference_pairs(original, events)
        self.assertEqual(completed["refs"], ["E000001", "E000002"])
        self.assertEqual(original["refs"], ["E000001"])
        self.assertEqual(changes[0]["added"], ["E000002"])
        self.assertFalse(events[1]["success"])
        events.append({"ref": "E000004", "type": "tool.execution_start", "tool_call_id": "call-1"})
        self.assertEqual(complete_reference_pairs(original, events)[0], original)

    def test_coverage_cannot_add_assistant_options_as_user_inputs(self):
        candidate = article()
        candidate["human_input_coverage"].append({"ref": "E000002", "treatment": "User chose the tool result"})
        self.assertTrue(validate_article(candidate, packet()))

    def test_minor_suggestions_do_not_masquerade_as_fact_failures(self):
        draft = {"article": article(), "insights": insights(), "brief": brief()}
        review = edition_review()
        review["suggestions"] = [{"path": "/article", "reason": "Could be more concise"}]
        self.assertEqual(validate_grounding(review, draft, packet(), ""), [])
        review = edition_review([{"severity": "minor", "reason": "Could be more concise"}])
        self.assertTrue(validate_grounding(review, draft, packet(), ""))

    def test_acceptance_scope_has_a_required_review_and_grounded_repair(self):
        evidence = packet()
        evidence[0]["human_input"] += " 仅修改本地入口，不要提交或部署；以后集成前先复核入口。"
        draft = {"article": article(), "insights": insights(), "brief": brief()}
        route = draft["article"]["agent_detail"]["continuation"][0]
        route["done_when"] = "复核入口后必须集成部署才算完成。"
        field_path = "/article/agent_detail/continuation/0/done_when"
        review = edition_review([{"path": field_path, "quote": route["done_when"],
                                  "kind": "human_requirement", "category": "acceptance_scope",
                                  "reason": "条件性复核不能把用户排除的部署当成必要验收。",
                                  "evidence": [{"ref": "E000001", "origin": "human", "quote": "不要提交或部署"}]}])
        self.assertEqual(validate_grounding(review, draft, evidence, ""), [])
        missing_scope = edition_review()
        missing_scope["checked"] = [check for check in missing_scope["checked"] if check["category"] != "acceptance_scope"]
        self.assertTrue(validate_review(missing_scope))
        legacy = copy.deepcopy(missing_scope)
        legacy["checked"] = [check for check in legacy["checked"] if check["category"] != "data_minimization"]
        self.assertEqual(validate_review(legacy, protocol="grounded-findings/v2"), [])
        self.assertTrue(validate_review(edition_review(), protocol="grounded-findings/v2"))
        self.assertTrue(validate_review(edition_review(), protocol="unrecognized"))
        correction = "复核当前入口仍保留原有功能；部署不在本路线范围内。"
        backend = FakeBackend([review, {"patches": [{"op": "replace", "path": field_path, "value": correction}]}, edition_review()])
        with tempfile.TemporaryDirectory() as temporary:
            actual = generate_edition(Path(temporary), draft, evidence, backend, validate_article, language="auto")
            self.assertEqual(actual["article"]["agent_detail"]["continuation"][0]["done_when"], correction)
            self.assertEqual(actual["article"]["agent_detail"]["trajectory"], draft["article"]["agent_detail"]["trajectory"])
            self.assertIn("acceptance_scope", backend.prompts[0])
            self.assertIn("Specified local checks may suffice", backend.prompts[1])
            self.assertEqual(len(backend.calls), 3)
            receipt = json.loads((Path(temporary) / "edition-receipt.json").read_bytes())
            self.assertEqual(receipt["review_protocol"], "grounded-findings/v4")
            self.assertEqual(receipt["identity"]["review_protocol"], "grounded-findings/v4")

    def test_modality_and_unknown_coverage_use_existing_grounded_repair_budget(self):
        evidence = packet()
        evidence[0]["human_input"] += " 归档尚未执行。迁移前先检查表结构。不要删除快照。"
        evidence[1]["result"]["content"] += " Checks passed; input values and assertion bodies were not supplied."
        draft = {"article": article(), "insights": insights(), "brief": brief()}
        draft["article"]["agent_markdown"] += "\n\n不要删除快照。E000001。"
        detail = draft["article"]["agent_detail"]
        detail["resume"]["verification_boundary"] = "由于未进行归档，所以禁止归档。"
        detail["continuation"][0]["stop_when"] = "任何迁移请求都必须拒绝。"
        detail["paths"][0]["reuse_condition"] = "检查证明从未处理时区输入。"
        original = copy.deepcopy(draft)
        locations = ["/article/agent_detail/resume/verification_boundary",
                     "/article/agent_detail/continuation/0/stop_when",
                     "/article/agent_detail/paths/0/reuse_condition"]
        quoted = [detail["resume"]["verification_boundary"], detail["continuation"][0]["stop_when"],
                  detail["paths"][0]["reuse_condition"]]
        contract_quote = "Unspecified test inputs establish neither coverage nor noncoverage."
        findings = [
            {"path": locations[0], "quote": quoted[0], "kind": "human_requirement", "category": "acceptance_scope",
             "reason": "未执行不能变成禁令。", "evidence": [{"ref": "E000001", "origin": "human", "quote": "归档尚未执行。"}]},
            {"path": locations[1], "quote": quoted[1], "kind": "human_requirement", "category": "acceptance_scope",
             "reason": "前置条件不是绝对禁止。", "evidence": [{"ref": "E000001", "origin": "human", "quote": "迁移前先检查表结构。"}]},
            {"path": locations[2], "quote": quoted[2], "kind": "contract", "category": "evidence_strength",
             "reason": "未提供输入，覆盖未知。", "contract_quote": contract_quote, "evidence": []},
        ]
        corrected = ["归档尚未执行；这不表示禁止，具体权限仍按当前约定。",
                     "迁移前未检查表结构时暂停；这不要求现在迁移。",
                     "记录未提供检查输入，时区输入的覆盖情况未确认。"]
        patches = [{"op": "replace", "path": path, "value": value} for path, value in zip(locations, corrected)]
        backend = FakeBackend([edition_review(findings), {"patches": patches}, edition_review()])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            actual = generate_edition(root, draft, evidence, backend, validate_article, max_repairs=1)
            receipt = json.loads((root / "edition-receipt.json").read_bytes())
        self.assertEqual(actual["article"]["agent_detail"]["resume"]["verification_boundary"], corrected[0])
        self.assertEqual(actual["article"]["agent_detail"]["continuation"][0]["stop_when"], corrected[1])
        self.assertEqual(actual["article"]["agent_detail"]["paths"][0]["reuse_condition"], corrected[2])
        self.assertEqual(actual["article"]["agent_markdown"], original["article"]["agent_markdown"])
        self.assertEqual(actual["article"]["agent_detail"]["trajectory"], original["article"]["agent_detail"]["trajectory"])
        self.assertEqual(draft, original)
        self.assertIn(contract_quote, backend.prompts[0])
        self.assertIn("source_modality_and_coverage", backend.prompts[0])
        self.assertEqual([call["label"] for call in backend.calls],
                         ["story-edition-review", "story-edition-patch", "story-edition-review"])
        self.assertEqual(receipt["repair_limits"], {"editorial": 1, "structural": 0})
        self.assertEqual(receipt["repair_counts"]["patches"], 1)

    def test_generic_source_strength_repairs_use_one_existing_patch_without_erasing_history(self):
        from session_spec.story_grounding import pointer_value
        evidence = packet()
        evidence[0]["human_input"] += " 重试记录会丢失吗？不要丢失记录，具体表示方式尚未决定。顺便说，今天的云很好看。"
        evidence[1]["result"]["content"] += " 已定位重试分支的缺口，尚未修改。本次未触发重试。文件名列表：worker.py，config.toml。"
        original_evidence = copy.deepcopy(evidence)
        draft = {"article": article(), "insights": insights(), "brief": brief()}
        draft["article"]["title"] = "重试缺口已经修复"
        draft["article"]["subtitle"] = "修复已经完成"
        draft["article"]["opening"] = "用户原先相信记录必定丢失，后来该信念被推翻。"
        detail = draft["article"]["agent_detail"]
        detail["continuation"][0]["done_when"] = "记录不丢失；若自行认定预期如此，丢失也算成功。"
        detail["recipes"][0]["verify"] = "允许丢失记录，只要把它称为预期行为。"
        detail["recipes"][0]["avoid"] = "本次未触发重试，便可删除重试分支。"
        draft["article"]["agent_markdown"] += "\n\n文件名列表证明项目没有状态存储行为。E000002。"
        draft["article"]["chapters"][0]["markdown"] += " 本次已省略一段闲聊。"
        draft["insights"]["closing"]["paragraphs"][0] = "这个失败的旧信念教会用户不能信任名称。"
        changes = [
            ("/article/title", "重试缺口已经修复", "重试缺口仍待处理", "evidence_strength", "Diagnosis is not implementation."),
            ("/article/subtitle", "修复已经完成", "已完成定位，尚未修改", "evidence_strength", "Titles and decks require the same evidence strength as the body."),
            ("/article/agent_detail/continuation/0/done_when", detail["continuation"][0]["done_when"],
             "记录不丢失；表示方式未定，不能把未定政策当作验收例外。", "acceptance_scope",
             "Acceptance must distinguish the reported failure from success across continuation and recipes."),
            ("/article/agent_detail/recipes/0/verify", detail["recipes"][0]["verify"],
             "检查记录是否丢失；表示方式仍需决定，不预设某种形式是用户要求。", "acceptance_scope",
             "Unresolved representation choices remain proposed, not user requirements."),
            ("/article/agent_detail/recipes/0/avoid", detail["recipes"][0]["avoid"],
             "本次未触发不证明分支无用；先核对输入域与预期行为。", "mechanism",
             "Current nonoccurrence is not impossibility or proof of dead logic."),
            ("/article/agent_markdown", "文件名列表证明项目没有状态存储行为。", "该列表只记录文件名，不能推断未读内容。",
             "evidence_strength", "A filename listing is not a content inspection."),
            ("/article/chapters/0/markdown", " 本次已省略一段闲聊。", "", "narrative_and_scope",
             "Omit irrelevant non-task asides without narrating their presence or omission."),
            ("/article/opening", draft["article"]["opening"], "用户询问重试记录是否丢失，并说明不能丢失记录。",
             "evidence_strength", "Questioning a proposition does not establish belief in it or its disproof."),
            ("/insights/closing/paragraphs/0", draft["insights"]["closing"]["paragraphs"][0],
             "遇到记录完整性问题时，应检查实际输入和行为；仅凭名称不能确定值或历史信念。", "evidence_strength",
             "Types and identifiers do not establish actual values or historical beliefs."),
        ]
        original = copy.deepcopy(draft)
        findings = [{"path": path, "quote": old, "kind": "contract", "category": category,
                     "reason": "保持来源范围与证明力度，不补造事实、政策或旁白。", "contract_quote": rule, "evidence": []}
                    for path, old, new, category, rule in changes]
        patches = [{"op": "replace_text", "path": path, "old": old, "value": new}
                   for path, old, new, category, rule in changes]
        backend = FakeBackend([edition_review(findings), {"patches": patches}, edition_review()])
        with tempfile.TemporaryDirectory() as temporary:
            actual = generate_edition(Path(temporary), draft, evidence, backend, validate_article, max_repairs=1)
            receipt = json.loads((Path(temporary) / "edition-receipt.json").read_bytes())
        for path, old, new, category, rule in changes:
            self.assertEqual(pointer_value(actual, path), pointer_value(original, path).replace(old, new))
        self.assertEqual(actual["article"]["human_input_coverage"], original["article"]["human_input_coverage"])
        self.assertEqual(actual["article"]["agent_detail"]["trajectory"], original["article"]["agent_detail"]["trajectory"])
        self.assertEqual(evidence, original_evidence)
        self.assertEqual(draft, original)
        self.assertEqual([call["label"] for call in backend.calls],
                         ["story-edition-review", "story-edition-patch", "story-edition-review"])
        self.assertEqual(receipt["repair_limits"], {"editorial": 1, "structural": 0})
        self.assertEqual(receipt["repair_counts"]["patches"], 1)

    def test_modality_grounding_rejects_invented_authority_and_contracts(self):
        evidence = packet()
        evidence[0]["human_input"] += " 不要删除快照。"
        evidence.append({"ref": "E000003", "type": "assistant.message", "text": "归档尚未执行。"})
        draft = {"article": article(), "insights": insights(), "brief": brief()}
        source_issue = edition_review([{"kind": "human_requirement", "category": "acceptance_scope",
                                       "evidence": [{"ref": "E000001", "origin": "human", "quote": "不要删除快照。"}]}])
        self.assertEqual(validate_grounding(source_issue, draft, evidence, ""), [])
        for support in ({"ref": "E000003", "origin": "human", "quote": "归档尚未执行。"},
                        {"ref": "E000003", "origin": "assistant", "quote": "归档尚未执行。"},
                        {"ref": "E000001", "origin": "human", "quote": "任何归档都被禁止。"}):
            with self.subTest(support=support):
                invalid = copy.deepcopy(source_issue)
                invalid["issues"][0]["evidence"] = [support]
                self.assertTrue(validate_grounding(invalid, draft, evidence, ""))
        contract = (PROMPTS / "agent-detail.md").read_text(encoding="utf-8")
        contract_issue = edition_review([{"kind": "contract", "category": "evidence_strength", "evidence": [],
                                          "contract_quote": "Unspecified test inputs establish neither coverage nor noncoverage."}])
        self.assertEqual(validate_grounding(contract_issue, draft, evidence, contract), [])
        contract_issue["issues"][0]["contract_quote"] = "Every unmentioned behavior is forbidden."
        self.assertTrue(validate_grounding(contract_issue, draft, evidence, contract))

    def test_modality_and_redundancy_suggestions_preserve_supported_boundaries(self):
        evidence = packet()
        evidence[0]["human_input"] += " 不要删除快照。先检查表结构，再考虑是否迁移。"
        evidence[1]["result"]["content"] += " This recorded run did not exercise time-zone inputs. New checks were added in this change."
        draft = {"article": article(), "insights": insights(), "brief": brief()}
        draft["article"]["agent_markdown"] += "\n\n不要删除快照；迁移前检查表结构。E000001。"
        detail = draft["article"]["agent_detail"]
        detail["resume"]["verification_boundary"] = "工具明确报告本次未检查时区输入，不推及其他运行。"
        detail["continuation"][0]["steps"][0]["action"] = "检查实现和本次新增检查的相关差异，再判断下一步。"
        detail["paths"][0]["reuse_condition"] = "保留禁止删除快照的约束；未来迁移仍有前置条件。"
        original = copy.deepcopy(draft)
        review = edition_review()
        review["suggestions"] = [{"path": "/article/agent_markdown", "reason": "可压缩重复解释，保留操作边界和失败历史。"}]
        backend = FakeBackend([review])
        with tempfile.TemporaryDirectory() as temporary:
            actual = generate_edition(Path(temporary), draft, evidence, backend, validate_article)
            receipt = json.loads((Path(temporary) / "edition-receipt.json").read_bytes())
        self.assertEqual(actual, original)
        self.assertEqual(draft, original)
        self.assertEqual([call["label"] for call in backend.calls], ["story-edition-review"])
        self.assertEqual(receipt["repair_counts"]["patches"], 0)
        self.assertIn("agent_section_responsibilities", backend.prompts[0])

    def test_imported_skill_help_is_not_promoted_to_human_authority(self):
        records = [
            {"ref": "E000001", "origin": "root", "type": "tool.execution_complete", "tool": "Skill", "timestamp": "2026-01-01T00:00:00.100+00:00", "result": {"content": "Launching skill: docs:help"}},
            {"ref": "E000002", "origin": "root", "type": "user.message", "timestamp": "2026-01-01T00:00:00.099+00:00", "text": "# Help\n" + "Historical documentation. " * 8, "import_metadata": {"role": "user"}},
            {"ref": "E000003", "origin": "root", "type": "user.message", "timestamp": "2026-01-01T00:01:00+00:00", "text": "Please implement the requested fix.", "import_metadata": {"role": "user"}},
        ]
        result = root_packet(records)
        self.assertNotIn("human_input", result[1])
        self.assertIn("Historical documentation", result[1]["text"])
        self.assertEqual(result[1]["source_context"]["basis_refs"], ["E000001"])
        self.assertEqual(result[2]["human_input"], records[2]["text"])
        records[1]["timestamp"] = "2026-01-01T00:05:00+00:00"
        self.assertIn("human_input", root_packet(records)[1])

    def test_tool_exchange_index_preserves_pairs_without_inventing_success(self):
        records = [{"ref": "E000001", "origin": "root", "type": "tool.execution_start", "tool": "Bash", "tool_call_id": "pending", "text": ""},
                   {"ref": "E000002", "origin": "root", "type": "tool.execution_complete", "tool": "Bash", "tool_call_id": "separate", "text": ""}]
        result = root_packet(records)
        index = tool_exchanges(result)["by_call_id"]
        self.assertEqual(index["pending"]["results"], [])
        self.assertEqual(index["separate"]["requests"], [])
        self.assertNotIn("success", result[1])

    def test_include_and_omit(self):
        self.assertEqual(validate_insights(insights(), article(), packet()), [])
        self.assertEqual(validate_insights(insights(False), article(), packet()), [])
        self.assertEqual(validate_article(article(), packet()), [])

    def test_rejects_placeholder_graph_on_omit(self):
        candidate = insights(False)
        candidate["architecture"]["nodes"] = []
        self.assertTrue(validate_insights(candidate, article(), packet()))

    def test_bad_edges_references_and_claims_fail(self):
        for key, value in (("to", "missing"), ("basis", "proposed"), ("refs", ["E000999"]), ("refs", ["E000001"])):
            with self.subTest(key=key, value=value):
                candidate = insights()
                candidate["architecture"]["edges"][0][key] = value
                self.assertTrue(validate_insights(candidate, article(), packet()))

    def test_orphan_node_and_ungrounded_closing_fail(self):
        candidate = insights()
        candidate["architecture"]["nodes"].append({"id": "unused", "title": "猜测", "role": "处理", "detail": "不存在的关系", "refs": ["E000002"]})
        candidate["closing"]["anchors"] = []
        self.assertGreaterEqual(len(validate_insights(candidate, article(), packet())), 2)

    def test_malformed_enums_do_not_crash(self):
        candidate = insights()
        candidate["architecture"]["kind"] = []
        candidate["architecture"]["nodes"][0]["role"] = {}
        candidate["architecture"]["edges"][0]["kind"] = []
        self.assertTrue(validate_insights(candidate, article(), packet()))

    def test_disconnected_real_chains_are_allowed(self):
        candidate = insights()
        second = copy.deepcopy(candidate["architecture"]["nodes"])
        for node in second:
            node["id"] += "-second"
        candidate["architecture"]["nodes"].extend(second)
        edge = copy.deepcopy(candidate["architecture"]["edges"][0])
        edge.update({"from": "task-second", "to": "wrapper-second"})
        candidate["architecture"]["edges"].append(edge)
        self.assertEqual(validate_insights(candidate, article(), packet()), [])

    def test_citations_and_user_coverage_not_exposed_or_lost(self):
        candidate = article()
        candidate["chapters"][0]["markdown"] += " E000002"
        candidate["human_input_coverage"] = [{"ref": "E000002", "treatment": "wrong"}]
        self.assertGreaterEqual(len(validate_article(candidate, packet())), 2)

    def test_malformed_reference_arrays_are_rejected(self):
        for references in (["not-an-evidence-id"], "E000002", [None], [{}]):
            candidate = article()
            candidate["chapters"][0]["refs"] = references
            self.assertTrue(validate_article(candidate, packet()))

    def test_ported_file_ledger_tracks_requests_not_success(self):
        events = [{"type": "tool.execution_start", "tool": tool, "arguments": {"path": filename}, "ref": "E000001"}
                  for tool, filename in (("read", "alpha"), ("write", "alpha"), ("read", "beta"), ("edit", "gamma"))]
        ledger = file_activity(events)
        self.assertEqual(ledger["read_only_requests"], ["beta"])
        self.assertEqual(ledger["write_or_edit_requests"], ["alpha", "gamma"])
        self.assertIn("not proof", ledger["limit"])

    def test_file_ledger_accepts_case_variants_without_claiming_completion(self):
        events = [{"type": "tool.execution_start", "tool": "Read", "arguments": {"file_path": "alpha.py"}, "ref": "E000001"},
                  {"type": "tool.execution_start", "tool": "Edit", "arguments": {"file_path": "alpha.py"}, "ref": "E000002"}]
        ledger = file_activity(events)
        self.assertEqual(ledger["read_only_requests"], [])
        self.assertEqual(ledger["write_or_edit_requests"], ["alpha.py"])
        self.assertEqual(ledger["refs_by_path"]["alpha.py"], ["E000001", "E000002"])

    def test_ported_windows_budget_preserves_chronology(self):
        chunks = [{"turn": turn, "insight_score": score, "messages": [{"ref": "E000001", "text": text}]}
                  for turn, score, text in ((3, 9, "x" * 20), (2, 5, "short"), (1, 1, "first"))]
        self.assertEqual([window["turn"] for window in select_story_windows(chunks, 10)], [1, 2])

    def test_insights_cache_is_bound_to_inputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            write_json(directory / "article.json", article())
            write_json(directory / "input.json", packet())
            backend = FakeBackend([insights(False), {"issues": []}])
            generate_insights(directory, backend=backend)
            generate_insights(directory, backend=backend)
            self.assertEqual(len(backend.calls), 2)
            changed = article()
            changed["title"] = "另一个问题"
            write_json(directory / "article.json", changed)
            another = FakeBackend([{"issues": []}])
            generate_insights(directory, backend=another)
            self.assertEqual(len(another.calls), 1)
            changed_events = packet()
            changed_events[0]["text"] = "不同的源材料"
            write_json(directory / "input.json", changed_events)
            fresh = FakeBackend([insights(False), {"issues": []}])
            generate_insights(directory, backend=fresh)
            self.assertEqual(len(fresh.calls), 2)

    def test_failed_review_publishes_no_insights(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            write_json(directory / "article.json", article())
            write_json(directory / "input.json", packet())
            backend = FakeBackend([insights(), {"issues": [{"reason": "not supported"}]}])
            with self.assertRaises(ValueError):
                generate_insights(directory, backend=backend, max_repairs=0)
            self.assertFalse((directory / "insights.json").exists())

    def test_insights_repairs_only_the_bad_field(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            write_json(directory / "article.json", article())
            write_json(directory / "input.json", packet())
            invalid = insights()
            invalid["architecture"]["edges"][0]["basis"] = "proposed"
            backend = FakeBackend([invalid, {"patches": [{"op": "replace", "path": "/architecture/edges/0/basis", "value": "implementation"}]}, {"issues": []}])
            actual = generate_insights(directory, backend=backend)
            self.assertEqual(actual, insights())
            self.assertEqual(len(backend.calls), 3)

    def test_writer_resumes_latest_draft_without_rewriting_good_fields(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            draft = article()
            draft["title"] = "最新的未发布稿"
            write_json(directory / "article-candidate-2.json", draft)
            backend = FakeBackend([{"issues": [{"reason": "Fix only the title"}]}, {"patches": [{"op": "replace", "path": "/title", "value": "修正后的标题"}]}, {"issues": []}])
            actual = reviewed_article(directory, packet(), None, backend, None)
            self.assertEqual(actual["title"], "修正后的标题")
            self.assertEqual(actual["chapters"], draft["chapters"])
            self.assertEqual(actual["agent_markdown"], draft["agent_markdown"])
            self.assertEqual(len(backend.calls), 3)

    def test_previously_rejected_draft_cannot_pass_unchanged_on_resume(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            draft = article()
            write_json(directory / "article-candidate-2.json", draft)
            write_json(directory / "article-review-2.json", {"issues": [{"reason": "The title has an unsupported claim"}]})
            backend = FakeBackend([{"patches": [{"op": "replace", "path": "/title", "value": "有依据的新标题"}]}, {"issues": []}])
            actual = reviewed_article(directory, packet(), None, backend, None)
            self.assertEqual(actual["title"], "有依据的新标题")
            self.assertEqual(backend.calls[0]["label"], "human-story-patch")
            self.assertEqual(len(backend.calls), 2)


class StoryBriefTests(unittest.TestCase):
    def test_explicit_exclusions_require_human_quote(self):
        self.assertEqual(validate_brief(brief(), packet()), [])
        events = packet()
        events[0]["human_input"] = "不卸载软件，只隐藏窗口。"
        candidate = brief()
        candidate["non_goals"] = [{"text": "不卸载现有软件。", "quote": "不卸载软件", "refs": ["E000001"]}]
        self.assertEqual(validate_brief(candidate, events), [])
        for replacement in ({"quote": "未经记录的排除"}, {"refs": ["E000002"]}, {"refs": []}):
            changed = copy.deepcopy(candidate)
            changed["non_goals"][0].update(replacement)
            self.assertTrue(validate_brief(changed, events))

    def test_intent_requires_human_source_and_prose_has_no_internal_ids(self):
        for field in ("goals", "constraints"):
            candidate = brief()
            candidate[field][0]["refs"] = ["E000002"]
            self.assertTrue(validate_brief(candidate, packet()))
        for replacement in ({"text": "参见 E000001"}, {"text": "<script>alert(1)</script>"}, {"refs": [{}]}, {"refs": ["E000999"]}):
            candidate = brief()
            candidate["problem"].update(replacement)
            self.assertTrue(validate_brief(candidate, packet()))

    def test_missing_and_malformed_fields_are_not_published(self):
        for field, replacement in (("schema", "wrong"), ("problem", None), ("goals", []), ("non_goals", "unknown"), ("constraints", [{}]), ("scope", [None])):
            candidate = brief()
            candidate[field] = replacement
            self.assertTrue(validate_brief(candidate, packet()))

    def test_cache_tracks_source_article_model_and_prompt_without_changing_article(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            write_json(directory / "article.json", article())
            write_json(directory / "input.json", packet())
            original = (directory / "article.json").read_bytes()
            backend = FakeBackend([brief(), {"issues": []}])
            generate_brief(directory, backend)
            generate_brief(directory, backend)
            self.assertEqual(len(backend.calls), 2)
            self.assertEqual((directory / "article.json").read_bytes(), original)
            changed = article()
            changed["title"] = "新的文章标题"
            write_json(directory / "article.json", changed)
            backend = FakeBackend([brief(), {"issues": []}])
            generate_brief(directory, backend)
            self.assertEqual(len(backend.calls), 2)
            events = packet()
            events[1]["result"] = {"content": "Different readback"}
            write_json(directory / "input.json", events)
            backend = FakeBackend([brief(), {"issues": []}])
            generate_brief(directory, backend)
            self.assertEqual(len(backend.calls), 2)
            backend = FakeBackend([{"issues": []}])
            generate_brief(directory, backend, model="different")
            self.assertEqual(len(backend.calls), 1)
            templates = directory / "templates"
            templates.mkdir()
            for name in ("story-brief.md", "story-brief-review.md"):
                shutil.copy2(PROMPTS / name, templates / name)
            with (templates / "story-brief.md").open("a", encoding="utf-8") as stream:
                stream.write("\nRevised writing contract\n")
            backend = FakeBackend([{"issues": []}])
            with patch("session_spec.story_brief.PROMPTS", templates):
                generate_brief(directory, backend, model="different")
            self.assertEqual(len(backend.calls), 1)

    def test_failed_candidate_cannot_pass_unchanged_after_resume(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            write_json(directory / "article.json", article())
            write_json(directory / "input.json", packet())
            backend = FakeBackend([brief(), {"issues": [{"reason": "Clarify the observation limit"}]}])
            with self.assertRaises(ValueError):
                generate_brief(directory, backend, max_repairs=0)
            self.assertFalse((directory / "brief.json").exists())
            backend = FakeBackend([{"patches": [{"op": "replace", "path": "/status/text", "value": "只看到启动信号。"}]}, {"issues": []}])
            result = generate_brief(directory, backend)
            self.assertEqual(backend.calls[0]["label"], "story-brief-patch")
            self.assertEqual(result["status"]["text"], "只看到启动信号。")
            self.assertEqual(result["background"], brief()["background"])
            self.assertEqual(json.loads((directory / "article.json").read_bytes()), article())

    def test_invalid_review_or_out_of_scope_patch_keeps_accepted_brief(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            write_json(directory / "article.json", article())
            write_json(directory / "input.json", packet())
            generate_brief(directory, FakeBackend([brief(), {"issues": []}]))
            accepted = (directory / "brief.json").read_bytes()
            backend = FakeBackend([{"issues": "malformed"}])
            with self.assertRaises(ValueError):
                generate_brief(directory, backend, model="different")
            self.assertEqual((directory / "brief.json").read_bytes(), accepted)
            backend = FakeBackend([{"issues": [{"reason": "Change status"}]}, {"patches": [{"op": "replace", "path": "/agent_markdown", "value": "not allowed"}]}])
            with self.assertRaises(ValueError):
                generate_brief(directory, backend, model="different", max_repairs=1)
            self.assertEqual((directory / "brief.json").read_bytes(), accepted)


class StoryEditorTests(unittest.TestCase):
    def test_feedback_is_source_bound_and_each_finding_needs_resolution(self):
        feedback = {"source_sha256": "source-hash", "issues": [{"reason": "A contradictory claim", "refs": ["E000001"]}]}
        self.assertEqual(validate_feedback(feedback, packet(), "source-hash"), feedback["issues"])
        with self.assertRaises(ValueError):
            validate_feedback(feedback, packet(), "different-session")
        wrong = copy.deepcopy(feedback)
        wrong["issues"][0]["refs"] = ["E000999"]
        with self.assertRaises(ValueError):
            validate_feedback(wrong, packet(), "source-hash")
        review = edition_review()
        self.assertTrue(validate_review(review, feedback["issues"]))
        review["feedback_resolution"] = [{"index": 0, "status": "needs_fix", "note": "Still present"}]
        self.assertTrue(validate_review(review, feedback["issues"]))
        review["feedback_resolution"][0].update(status="fixed", note="Replaced with the observed state")
        self.assertEqual(validate_review(review, feedback["issues"]), [])

    def test_new_reader_feedback_invalidates_an_earlier_empty_review(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            draft = {"article": article(), "insights": insights(), "brief": brief()}
            generate_edition(directory, draft, packet(), FakeBackend([edition_review()]), validate_article)
            finding = [{"reason": "Recheck this newly noticed issue", "refs": ["E000001"]}]
            review = edition_review()
            review["feedback_resolution"] = [{"index": 0, "status": "not_applicable", "note": "Current paragraph already preserves the boundary"}]
            backend = FakeBackend([review])
            generate_edition(directory, draft, packet(), backend, validate_article, feedback=finding)
            self.assertEqual(len(backend.calls), 1)
            self.assertIn("Recheck this newly noticed issue", backend.prompts[0])
            self.assertIn("feedback_resolution", backend.prompts[0].splitlines()[-1])

    def test_cli_dry_run_reports_whole_document_stage_and_allows_cache_only_budget(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = cli_main(["story", "--from-export", "unused-source", "--out", "unused-output", "--dry-run", "--max-calls", "0"])
        self.assertEqual(result, 0)
        report = json.loads(output.getvalue())
        self.assertEqual(report["additional_story_calls_minimum"], 3)
        self.assertEqual(report["additional_story_calls_with_repairs_maximum"], 0)
        self.assertEqual(report["matching_cache_calls_minimum"], 0)
        self.assertEqual(report["language"], "auto")
        self.assertTrue(any("whole-document" in stage for stage in report["stages"]))
        self.assertTrue(any("transfer probe" in stage for stage in report["stages"]))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = cli_main(["story", "--from-export", "unused-source", "--out", "unused-output", "--dry-run", "--max-calls", "8", "--revise-from", "unused-prior"])
        self.assertEqual(result, 0)
        report = json.loads(output.getvalue())
        self.assertEqual(report["additional_story_calls_minimum"], 2)
        self.assertEqual(report["additional_story_calls_with_repairs_maximum"], 8)
        self.assertIn("shared --max-calls budget", report["note"])

    def test_compaction_only_removes_identical_duplicate_result_text(self):
        evidence = [{"result": {"content": "same", "detailedContent": "same"}}, {"result": {"content": "short", "detailedContent": "longer evidence"}}]
        original = copy.deepcopy(evidence)
        compact = compact_evidence(evidence)
        self.assertNotIn("detailedContent", compact[0]["result"])
        self.assertEqual(compact[1], evidence[1])
        self.assertEqual(evidence, original)

    def test_review_cannot_skip_reader_responsibilities(self):
        self.assertEqual(validate_review(edition_review()), [])
        self.assertTrue(validate_review({"issues": []}))
        review = edition_review()
        review["checked"].pop()
        self.assertTrue(validate_review(review))

    def test_edition_detects_length_route_and_handoff_order(self):
        draft = {"article": article(), "insights": insights(), "brief": brief()}
        self.assertEqual(validate_edition(draft, packet(), validate_article), [])
        draft["article"]["route"] *= 6
        draft["article"]["agent_markdown"] = "## 历史轨迹\n先介绍历史。E000001"
        for field in ("background", "problem", "approach", "status"):
            draft["brief"][field]["text"] = "很" * 300
        draft["brief"]["goals"][0]["text"] = "目标" * 150
        self.assertGreaterEqual(len(validate_edition(draft, packet(), validate_article)), 4)
        draft["insights"]["architecture"]["after_chapter"] = "not-in-the-edited-article"
        self.assertIn("Architecture chapter does not exist", validate_edition(draft, packet(), validate_article))

    def test_editor_repairs_across_components_then_caches_without_mutating_drafts(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            draft = {"article": article(), "insights": insights(), "brief": brief()}
            original = copy.deepcopy(draft)
            backend = FakeBackend([edition_review([{"reason": "Clarify the title and closing together"}]),
                                   {"patches": [{"op": "replace", "path": "/article/title", "value": "静默启动的边界"},
                                                {"op": "replace", "path": "/insights/closing/title", "value": "保留职责，改变入口"}]}, edition_review()])
            actual = generate_edition(directory, draft, packet(), backend, validate_article)
            self.assertEqual(actual["article"]["title"], "静默启动的边界")
            self.assertIn("STRUCTURAL_OUTPUT_CONTRACT", backend.prompts[1])
            self.assertIn('"nodes"', backend.prompts[1])
            self.assertEqual(actual["insights"]["closing"]["title"], "保留职责，改变入口")
            self.assertEqual(draft, original)
            cached = FakeBackend([])
            self.assertEqual(generate_edition(directory, draft, packet(), cached, validate_article), actual)
            self.assertEqual(cached.calls, [])
            changed_contract = FakeBackend([edition_review()])
            self.assertEqual(generate_edition(directory, draft, packet(), changed_contract, validate_article, model="different"), actual)

    def test_oversized_brief_is_not_fixed_by_shortening_unused_opening(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            draft = {"article": article(), "insights": insights(), "brief": brief()}
            for field in ("background", "problem", "approach", "status"):
                draft["brief"][field]["text"] = "很" * 300
            draft["brief"]["goals"][0]["text"] = "目标" * 150
            errors = validate_edition(draft, packet(), validate_article)
            self.assertTrue(any(issue.startswith("/brief:") for issue in errors))
            backend = FakeBackend([{"patches": [
                {"op": "replace", "path": "/article/opening", "value": "精简的备用导语。"}
            ]}, edition_review()])
            with self.assertRaises(ValueError):
                generate_edition(directory, draft, packet(), backend, validate_article, max_repairs=1)
            self.assertFalse((directory / "edition.json").exists())
            resumed = FakeBackend([{"patches": [
                {"op": "replace", "path": "/brief", "value": brief()}
            ]}, edition_review()])
            result = generate_edition(directory, draft, packet(), resumed, validate_article)
            self.assertEqual(result["brief"], brief())
            self.assertEqual(result["article"]["opening"], "精简的备用导语。")

    def test_known_failed_edition_cannot_pass_unchanged_on_resume(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            draft = {"article": article(), "insights": insights(), "brief": brief()}
            backend = FakeBackend([edition_review([{"reason": "Title and timing disagree"}])])
            with self.assertRaises(ValueError):
                generate_edition(directory, draft, packet(), backend, validate_article, max_repairs=0)
            self.assertFalse((directory / "edition.json").exists())
            resumed = FakeBackend([{"patches": [{"op": "replace", "path": "/article/title", "value": "无时间夸大的标题"}]}, edition_review()])
            actual = generate_edition(directory, draft, packet(), resumed, validate_article)
            self.assertEqual(actual["article"]["title"], "无时间夸大的标题")
            self.assertEqual(resumed.calls[0]["label"], "story-edition-patch")

    def test_out_of_scope_patch_cannot_replace_source_or_publish(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            draft = {"article": article(), "insights": insights(), "brief": brief()}
            backend = FakeBackend([edition_review([{"reason": "Fix a claim"}]), {"patches": [{"op": "add", "path": "/source", "value": "forged"}]}])
            with self.assertRaises(ValueError):
                generate_edition(directory, draft, packet(), backend, validate_article, max_repairs=1)
            self.assertFalse((directory / "edition.json").exists())


@unittest.skipUnless(shutil.which("node"), "Node.js required for HTML rendering")
class StoryPipelineTests(unittest.TestCase):
    def test_brief_renders_exclusions_and_inline_code_without_private_refs(self):
        renderer = Path(__file__).resolve().parents[1] / "session_spec/web/story-page.cjs"
        candidate = brief()
        candidate["non_goals"] = [{"text": "不移除已有功能。", "quote": "INTERNAL_QUOTE_ONLY", "refs": ["E000001"]}]
        candidate["approach"]["text"] = "入口调用 `wrapper`，正文不执行 <script>。"
        candidate["constraints"] = []
        conclusion = insights()
        conclusion["closing"]["paragraphs"] = ["保留 `wrapper` 的职责，不执行 <script>。"]
        payload = json.dumps({"article": article(), "insights": conclusion, "brief": candidate}, ensure_ascii=False)
        script = "const {renderArticle}=require(process.argv[1]); const data=JSON.parse(process.argv[2]); process.stdout.write(renderArticle(data.article,{category:'test'},'#','#','next',data.insights,data.brief));"
        result = subprocess.run(["node", "-e", script, str(renderer), payload], capture_output=True, text=True, encoding="utf-8", check=True)
        intro = result.stdout.split('id="story-brief"', 1)[1].split('</section>', 1)[0]
        self.assertIn("明确不做 · Non-goals", intro)
        self.assertIn("不移除已有功能。", intro)
        self.assertIn("<code>wrapper</code>", intro)
        self.assertIn("&lt;script&gt;", intro)
        self.assertNotIn("<script>", intro)
        self.assertNotIn("INTERNAL_QUOTE_ONLY", intro)
        self.assertNotIn("E000001", intro)
        self.assertNotIn("会话未明确约定不做事项", intro)
        self.assertNotIn("限制与前提", intro)
        ending = result.stdout.split('id="story-takeaway"', 1)[1].split('</section>', 1)[0]
        self.assertIn("<code>wrapper</code>", ending)
        self.assertIn("&lt;script&gt;", ending)
        self.assertNotIn("<script>", ending)
        self.assertNotIn("`wrapper`", ending)
        candidate["non_goals"] = []
        payload = json.dumps({"article": article(), "insights": insights(), "brief": candidate}, ensure_ascii=False)
        result = subprocess.run(["node", "-e", script, str(renderer), payload], capture_output=True, text=True, encoding="utf-8", check=True)
        intro = result.stdout.split('id="story-brief"', 1)[1].split('</section>', 1)[0]
        self.assertNotIn("Non-goals", intro)
        self.assertNotIn("brief-boundaries", intro)

    def test_diagram_wrap_preserves_short_identifiers_and_punctuation(self):
        renderer = Path(__file__).resolve().parents[1] / "session_spec/web/story-diagram.cjs"
        result = subprocess.run(["node", "-e", "const {wrap,plainText}=require(process.argv[1]); console.log(JSON.stringify(wrap(plainText('负责启动 `KeepAwake.vbs` 并等待。'),18)));", str(renderer)], capture_output=True, text=True, encoding="utf-8", check=True)
        lines = json.loads(result.stdout)
        self.assertTrue(any("KeepAwake.vbs" in line for line in lines))
        self.assertFalse(any(line.startswith("。") for line in lines))
        self.assertNotIn("`", "".join(lines))

    def test_full_pipeline_cache_tamper_and_failed_resume(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source/events.jsonl"
            source.parent.mkdir()
            source.write_bytes(b"historical session snapshot")
            export = root / "base"
            export.mkdir()
            write_json(export / "source.json", {"source_path": str(source), "snapshot_bytes": source.stat().st_size,
                                               "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest()})
            (export / "evidence.jsonl").write_text("\n".join(json.dumps(event) for event in packet()), encoding="utf-8")
            write_json(export / "spec.json", {"spec": {"old_error": "STALE_CANONICAL_CLAIM"}})
            output = root / "output"
            draft = {"article": article(), "insights": insights(), "brief": brief()}
            backend = FakeBackend([draft, transfer_review(), edition_review()])
            result = run_story(None, root / "copilot", output, from_export=export, backend_factory=lambda **options: backend)
            self.assertEqual(result["model_calls"], 3)
            for prompt in backend.prompts:
                self.assertIn("SOURCE_LANGUAGE_POLICY", prompt)
                self.assertNotIn("The output language is", prompt)
            language_info = json.loads((output / "_support/language.json").read_bytes())
            self.assertEqual(language_info["requested"], "auto")
            self.assertEqual(language_info["language"], "zh-CN")
            self.assertIn("STALE_CANONICAL_CLAIM", backend.prompts[0])
            self.assertNotIn("STALE_CANONICAL_CLAIM", backend.prompts[1])
            self.assertTrue(validate_story(output)["valid"])
            html = (output / "human-spec.html").read_text(encoding="utf-8")
            self.assertIn('class="arch-svg"', html)
            self.assertIn('<code>agent-spec.md</code>', html)
            self.assertIn('<code>evidence.md</code>', html)
            self.assertNotIn('<dialog', html)
            self.assertNotIn('id="agent-preview"', html)
            self.assertIn('id="story-takeaway"', html)
            self.assertLess(html.index('id="story-brief"'), html.index('aria-label="故事路线"'))
            self.assertIn('href="#story-brief"', html)
            self.assertNotIn('class="opening"', html)
            intro = html.split('id="story-brief"', 1)[1].split('</section>', 1)[0]
            self.assertNotIn("Non-goals", intro)
            self.assertNotIn("会话未明确约定", intro)
            self.assertIn('class="brief-grid brief-boundaries"><div style="grid-column:1/-1">', intro)
            self.assertIn("限制与前提", intro)
            self.assertEqual((output / "agent-spec.md").read_text(encoding="utf-8"), render_agent(article(), packet(), "zh-CN"))
            self.assertNotIn("E000001", html.split('<dialog')[0])
            cached = FakeBackend([])
            run_story(None, root / "copilot", output, from_export=export, resume=True, backend_factory=lambda **options: cached)
            self.assertEqual(cached.calls, [])
            cached_report = json.loads((output / "_support/story-report.json").read_bytes())
            self.assertEqual(cached_report["previous_runs"][0]["calls"], backend.calls)
            self.assertEqual(cached_report["calls"], [])
            feedback_path = root / "feedback.json"
            source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
            write_json(feedback_path, {"source_sha256": source_hash, "issues": [{"reason": "Recheck the observed limit", "refs": ["E000001"]}]})
            feedback_review = edition_review()
            feedback_review["feedback_resolution"] = [{"index": 0, "status": "not_applicable", "note": "Current statement already distinguishes observation and acceptance"}]
            reviewed_feedback = FakeBackend([transfer_review(), feedback_review])
            with self.assertRaisesRegex(ValueError, "different identity"):
                run_story(None, root / "copilot", output, from_export=export, resume=True,
                          editorial_feedback=feedback_path, backend_factory=lambda **options: reviewed_feedback)
            self.assertEqual(reviewed_feedback.calls, [])
            self.assertTrue(validate_story(output)["valid"])
            original_output = output
            output = root / "revised-output"
            run_story(None, root / "copilot", output, from_export=export, revise_from=original_output,
                      editorial_feedback=feedback_path, backend_factory=lambda **options: reviewed_feedback)
            self.assertEqual(len(reviewed_feedback.calls), 2)
            self.assertTrue(validate_story(original_output)["valid"])
            self.assertTrue(validate_story(output)["valid"])
            retained = FakeBackend([])
            run_story(None, root / "copilot", output, from_export=export, resume=True, revise_from=original_output, backend_factory=lambda **options: retained)
            self.assertEqual(retained.calls, [])
            self.assertEqual(json.loads((output / "_support/editorial-feedback.json").read_bytes())["issues"][0]["reason"], "Recheck the observed limit")
            write_json(feedback_path, {"source_sha256": "wrong-source", "issues": []})
            with self.assertRaises(ValueError):
                run_story(None, root / "copilot", output, from_export=export, resume=True,
                          editorial_feedback=feedback_path, backend_factory=lambda **options: retained)
            self.assertTrue(validate_story(output)["valid"])
            self.assertEqual((output / "human-spec.html").read_text(encoding="utf-8"), html)
            changed_article = article()
            changed_article["title"] = "A changed but accepted draft"
            failing = FakeBackend([{**draft, "article": changed_article}, ValueError("joint review provider failed")])
            with self.assertRaises(ValueError):
                run_story(None, root / "copilot", output, from_export=export, resume=True, model="different", backend_factory=lambda **options: failing)
            self.assertTrue(validate_story(output)["valid"])
            self.assertEqual((output / "human-spec.html").read_text(encoding="utf-8"), html)
            failing_brief = FakeBackend([{"issues": "malformed"}, {"issues": "malformed"}])
            with self.assertRaises(ValueError):
                run_story(None, root / "copilot", output, from_export=export, resume=True, model="different", backend_factory=lambda **options: failing_brief)
            failed_attempt = json.loads((output / "_support/story-attempt.json").read_bytes())
            self.assertEqual(failed_attempt["previous_runs"][-1]["calls"], failing.calls)
            self.assertEqual(failed_attempt["previous_runs"][-1]["status"], "failed")
            self.assertEqual(failed_attempt["calls"], failing_brief.calls)
            self.assertTrue(validate_story(output)["valid"])
            self.assertEqual((output / "human-spec.html").read_text(encoding="utf-8"), html)
            brief_path = output / "_support/brief.json"
            accepted_brief = brief_path.read_bytes()
            tampered = brief()
            tampered["background"]["text"] = "Tampered brief"
            write_json(brief_path, tampered)
            self.assertFalse(validate_story(output)["valid"])
            brief_path.write_bytes(accepted_brief)
            brief_path.unlink()
            self.assertFalse(validate_story(output)["valid"])
            brief_path.write_bytes(accepted_brief)
            (output / "agent-spec.md").write_text("tampered", encoding="utf-8")
            self.assertFalse(validate_story(output)["valid"])
            self.assertEqual(source.read_bytes(), b"historical session snapshot")


if __name__ == "__main__":
    unittest.main()
