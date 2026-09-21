import copy
import json
import re
import uuid
from pathlib import Path

from .patching import apply_data_patches
from .story_brief import MAX_BRIEF_CHARS, MAX_TEXT_CHARS, validate_brief
from .story_insights import validate_insights
from .story_grounding import complete_reference_pairs, resolve_review_locations, validate_grounding
from .story_context import reference_role_guidance
from .storage import PROMPTS, digest, write_json
from .language import language_contract, source_language_reference, validate_language
from .model_io import generate_json
from .review_focus import SCHEMA as FOCUS_SCHEMA, review_focus
from .review_crosswalk import SCHEMA as CROSSWALK_SCHEMA, TRANSPORT_SCHEMA, review_crosswalk, review_crosswalk_transport
from .minimization_review import SCHEMA as MINIMIZATION_SCHEMA, minimization_focus, validate_minimization
from .assertion_scope import SCHEMA as ASSERTION_SCHEMA, assertion_scope
from .rationale_audit import SCHEMA as RATIONALE_SCHEMA, rationale_focus, validate_rationale_audit
from .transfer_probe import (ADJUDICATION, FINAL_POLICY, SCHEMA as PROBE_SCHEMA, contract as probe_contract, effective_feedback, feedback_context, final_pair_matches,
                             probe_feedback, resolve_feedback_locations, review_rules, run_probe, validate_resolutions)
from .agent_package import EVIDENCE_RENDERER


EDITION_SCHEMA = "story-edition/v2"
REVIEW_PROTOCOL = "grounded-findings/v4"
CHECKS = ("origin_and_goals", "narrative_and_scope", "readability", "evidence_strength", "mechanism", "agent_handoff", "acceptance_scope", "data_minimization")
PROTOCOL_CHECKS = {None: CHECKS[:6], "grounded-findings/v2": CHECKS[:6], "grounded-findings/v3": CHECKS[:7], REVIEW_PROTOCOL: CHECKS}


def fingerprint(value):
    return digest(json.dumps(value, ensure_ascii=False, sort_keys=True).encode())


def compact_evidence(value):
    if isinstance(value, list):
        return [compact_evidence(item) for item in value]
    if isinstance(value, dict):
        return {key: compact_evidence(item) for key, item in value.items()
                if not (key == "detailedContent" and item == value.get("content"))}
    return value


def brief_length(brief):
    if isinstance(brief, dict):
        return sum(len(value) if key == "text" and isinstance(value, str) else brief_length(value) for key, value in brief.items())
    if isinstance(brief, list):
        return sum(brief_length(item) for item in brief)
    return 0


def repair_invariants(edition, events, language):
    counts = []

    def collect(value, path):
        if isinstance(value, dict):
            for key, child in value.items():
                if key == "text" and isinstance(child, str):
                    counts.append({"path": path + "/text", "characters": len(child)})
                elif isinstance(child, (dict, list)):
                    collect(child, path + "/" + key)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                collect(child, path + "/" + str(index))

    collect(edition.get("brief", {}), "/brief")
    bounds = {"max_characters_per_text": MAX_TEXT_CHARS, "max_total_characters": MAX_BRIEF_CHARS,
              "current_total_characters": sum(item["characters"] for item in counts), "fields": counts}
    return ("\n\nREPAIR_INVARIANTS (all hold together; fixing one must not regress another)\n"
            + json.dumps(bounds, ensure_ascii=False)
            + "\nCounts are characters, including letters, spaces and punctuation, not words. "
            "Shorten clauses and remove repetition; do not translate into another language to meet a length bound. "
            "Keep required meaning and references; move supporting explanation to the relevant existing chapter instead of erasing a constraint. "
            "Preserve valid schemas, chapter refs and Agent inline evidence links; Human prose keeps refs only in internal fields. "
            "Do not relax validation, invent evidence or undo an earlier correction."
            + (source_language_reference(events) if language == "auto" else "") + language_contract(language))


def prior_review_findings(failures):
    findings = {}
    for issues in failures.values():
        for issue in issues:
            if issue.get("kind") in {"human_requirement", "source_fact", "contract"}:
                findings[fingerprint(issue)] = issue
    return list(findings.values())


def recorded_probe(edition, events, backend, directory, receipt, language, model):
    probe_directory = directory / ("reader-" + uuid.uuid4().hex[:12])
    probe_directory.mkdir(exist_ok=False)
    path = probe_directory / "edition-transfer-probe.json"
    entry = {"path": path.relative_to(directory).as_posix(), "status": "running"}
    receipt.setdefault("probe_artifacts", []).append(entry)
    write_json(directory / "edition-attempt.json", receipt)
    try:
        return run_probe(edition, events, backend, probe_directory, language, model)
    finally:
        if path.is_file():
            entry.update(sha256=digest(path.read_bytes()), status=json.loads(path.read_bytes()).get("status"))
        else:
            entry["status"] = "failed_before_record"
        write_json(directory / "edition-attempt.json", receipt)


def validate_probe_artifacts(directory, receipt):
    linked = [receipt.get("transfer_probe"), *receipt.get("followup_transfer_probes", [])]
    entries = receipt.get("probe_artifacts", [])
    if not isinstance(entries, list):
        raise ValueError("Invalid reader artifact history")
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise ValueError("Invalid reader artifact pointer")
        path = (directory / entry["path"]).resolve()
        if not path.is_relative_to(directory.resolve()) or path.name != "edition-transfer-probe.json":
            raise ValueError("Reader artifact escaped the review directory")
        if entry.get("status") == "failed_before_record" and not path.exists():
            continue
        if not path.is_file() or digest(path.read_bytes()) != entry.get("sha256"):
            raise ValueError("Reader artifact is missing, changed or interrupted before linkage; review its preserved record")
        record = json.loads(path.read_bytes())
        if record.get("status") == "completed" and record not in linked:
            raise ValueError("Completed reader artifact is not linked to active concerns; review its preserved record")


def validate_feedback(feedback, events, source_sha256):
    if not isinstance(feedback, dict) or feedback.get("source_sha256") != source_sha256:
        raise ValueError("Editorial feedback must be bound to this source snapshot")
    issues = feedback.get("issues")
    if not isinstance(issues, list) or len(issues) > 50 or len(json.dumps(feedback, ensure_ascii=False)) > 40000:
        raise ValueError("Editorial feedback must contain at most 50 bounded findings")
    references = {event["ref"] for event in events}
    for issue in issues:
        if not isinstance(issue, dict) or not isinstance(issue.get("reason"), str) or not issue["reason"].strip():
            raise ValueError("Editorial feedback needs a concrete reason for each finding")
        if issue.get("origin") == PROBE_SCHEMA:
            raise ValueError("External feedback cannot impersonate an internal transfer probe")
        refs = issue.get("refs", [])
        if not isinstance(refs, list) or any(not isinstance(ref, str) or ref not in references for ref in refs):
            raise ValueError("Editorial feedback contains unknown evidence references")
    return issues


def validate_review(review, feedback=None, protocol=REVIEW_PROTOCOL):
    if protocol is not None and not isinstance(protocol, str) or protocol not in PROTOCOL_CHECKS:
        return ["Unknown whole-document review protocol"]
    required_checks = PROTOCOL_CHECKS[protocol]
    if not isinstance(review, dict) or not isinstance(review.get("issues"), list):
        return ["Invalid whole-document review"]
    if any(not isinstance(issue, dict) or not isinstance(issue.get("reason"), str) or not issue["reason"].strip() for issue in review["issues"]):
        return ["Invalid whole-document findings"]
    checked = review.get("checked")
    if not isinstance(checked, list) or any(not isinstance(item, dict) or not isinstance(item.get("category"), str) or not isinstance(item.get("note"), str) or not item["note"].strip() for item in checked):
        return ["Whole-document review must explain every required check"]
    if sorted(item["category"] for item in checked) != sorted(required_checks):
        return ["Whole-document review coverage incomplete"]
    if feedback:
        resolutions = review.get("feedback_resolution")
        if not isinstance(resolutions, list) or any(not isinstance(item, dict) or not isinstance(item.get("index"), int)
            or item.get("status") not in ("fixed", "not_applicable", "needs_fix") or not isinstance(item.get("note"), str)
            or not item["note"].strip() for item in resolutions):
            return ["Editorial feedback needs an explicit resolution for every finding"]
        if sorted(item["index"] for item in resolutions) != list(range(len(feedback))):
            return ["Editorial feedback coverage incomplete"]
        if any(item["status"] == "needs_fix" for item in resolutions) and not review["issues"]:
            return ["Unresolved editorial feedback cannot pass an empty review"]
    return []


def validate_edition(edition, events, article_validator):
    if not isinstance(edition, dict) or set(edition) != {"article", "insights", "brief"}:
        return ["Edition must contain exactly article, insights and brief as SIBLING ROOT fields, not nested under article. Add any missing root object using its existing nested content; do not rewrite unrelated text."]
    article = edition["article"]
    article_errors = article_validator(article, events)
    errors = list(article_errors)
    errors += validate_brief(edition["brief"], events)
    if isinstance(article, dict) and isinstance(article.get("chapters"), list) and all(isinstance(chapter, dict) and isinstance(chapter.get("id"), str) for chapter in article["chapters"]):
        errors += validate_insights(edition["insights"], article, events)
    if article_errors:
        return errors
    if len(article["route"]) > 5:
        errors.append("Reading route must have at most five meaningful turning points, not every operation")
    if not article["agent_markdown"].lstrip().startswith("# "):
        errors.append("Agent handoff needs a document title and a current handoff summary before history")
    first_heading = re.search(r"^##\s+(.+)$", article["agent_markdown"], re.MULTILINE)
    if first_heading and re.search(r"历史|轨迹|来源|证据|history|trajectory|sources", first_heading.group(1), re.IGNORECASE):
        errors.append("Agent handoff must start with handoff state, not history or evidence navigation")
    return errors


def generate_edition(directory, draft, events, backend, article_validator, model=None, prior_findings=None, feedback=None, max_repairs=2, language="auto", max_structural_repairs=0, transfer_probe=False, *, crosswalk_transport=False):
    directory = Path(directory)
    contract = (PROMPTS / "story-editor.md").read_text(encoding="utf-8")
    contract += "\n\n" + (PROMPTS / "story-editor-method.md").read_text(encoding="utf-8")
    contract += "\n\n" + (PROMPTS / "agent-detail.md").read_text(encoding="utf-8")
    contract += reference_role_guidance(events)
    if language:
        contract += language_contract(language)
    structure_contract = (PROMPTS / "story-draft.md").read_text(encoding="utf-8")
    brief_contract = (PROMPTS / "story-brief.md").read_text(encoding="utf-8")
    brief_review = (PROMPTS / "story-brief-review.md").read_text(encoding="utf-8")
    identity = {"schema": EDITION_SCHEMA, "review_protocol": REVIEW_PROTOCOL, "review_focus": FOCUS_SCHEMA, "review_crosswalk": CROSSWALK_SCHEMA, "minimization_focus": MINIMIZATION_SCHEMA, "assertion_scope": ASSERTION_SCHEMA, "rationale_audit": RATIONALE_SCHEMA, "draft_sha256": fingerprint(draft), "input_sha256": fingerprint(events),
                "contract_sha256": digest((contract + brief_contract + brief_review + structure_contract).encode()),
                "model": model or "copilot-default", "prior_findings_sha256": fingerprint(prior_findings or []),
                "feedback_sha256": fingerprint(feedback or [])}
    if crosswalk_transport:
        identity["review_crosswalk_transport"] = TRANSPORT_SCHEMA
    if transfer_probe:
        identity.update(transfer_probe=PROBE_SCHEMA, transfer_adjudication=ADJUDICATION, transfer_probe_contract_sha256=digest(probe_contract(language).encode()),
                        evidence_renderer=EVIDENCE_RENDERER, final_transfer_policy=FINAL_POLICY)
    output, receipt_path = directory / "edition.json", directory / "edition-receipt.json"
    if output.is_file() and receipt_path.is_file():
        receipt = json.loads(receipt_path.read_bytes())
        edition = json.loads(output.read_bytes())
        try:
            accepted_feedback = effective_feedback(receipt, feedback, events, edition, language)
            if transfer_probe:
                review_rules(receipt)
            feedback_errors = validate_resolutions(receipt.get("review", {}), accepted_feedback, edition, events, contract + brief_contract + brief_review)
            feedback_errors += validate_minimization(receipt.get("review", {}), edition, events, minimization_focus(edition, events))
            feedback_errors += validate_rationale_audit(receipt.get("review", {}), rationale_focus(edition, events), events)
        except ValueError as error:
            accepted_feedback, feedback_errors = [], [str(error)]
        if (receipt.get("identity") == identity and receipt.get("status") == "completed"
                and receipt.get("output_sha256") == digest(output.read_bytes())
                and not validate_edition(edition, events, article_validator) and not validate_language(edition, language, events)
                and not feedback_errors and not validate_review(receipt.get("review"), accepted_feedback) and not receipt["review"]["issues"]):
            print("[edition cached] whole-document review", flush=True)
            return edition
    attempt_path, candidate_path = directory / "edition-attempt.json", directory / "edition-candidate.json"
    limits = {"editorial": max_repairs, "structural": max_structural_repairs}
    receipt = {"identity": identity, "status": "running", "failure_protocol": "editorial-only/v1", "failures": {}, "attempts": [],
               "repair_limits": limits, "repair_counts": {"patches": 0, "editorial": 0, "structural": 0}}
    if transfer_probe:
        receipt["review_contract"] = {"schema": "editor-contract/v1", "rules": contract + brief_contract + brief_review, "structure": structure_contract}
    edition = draft
    migrated_findings = []
    restored_feedback = None
    if attempt_path.is_file() and not candidate_path.is_file():
        previous = json.loads(attempt_path.read_bytes())
        if previous.get("identity", {}).get("final_transfer_policy"):
            raise ValueError("Cannot resume final-reader history without its bound candidate; use a new output directory")
    if attempt_path.is_file() and candidate_path.is_file():
        previous = json.loads(attempt_path.read_bytes())
        receipt["previous_runs"] = [*previous.get("previous_runs", []),
                                    {key: value for key, value in previous.items() if key != "previous_runs"}]
        previous_identity = previous.get("identity", {})
        reusable = all(previous_identity.get(key) == identity[key] for key in ("schema", "draft_sha256", "input_sha256"))
        if previous_identity.get("final_transfer_policy"):
            if previous_identity != identity or previous.get("candidate_sha256") != digest(candidate_path.read_bytes()):
                raise ValueError("Cannot rebind prior reader history to a different identity or candidate; use a new output directory")
            review_rules(previous)
            validate_probe_artifacts(directory, previous)
            if "transfer_probe" in previous:
                restored_feedback = effective_feedback(previous, feedback, events, require_final=False)
                for key in ("transfer_probe", "followup_transfer_probes", "effective_feedback"):
                    if key in previous:
                        receipt[key] = copy.deepcopy(previous[key])
            counts = previous.get("repair_counts")
            if (previous.get("repair_limits") != limits or not isinstance(counts, dict)
                    or set(counts) != {"patches", "editorial", "structural"}
                    or any(type(value) is not int or value < 0 for value in counts.values())
                    or counts["patches"] > max_repairs + max_structural_repairs):
                raise ValueError("Cannot resume an unbound or changed repair budget; use a new output directory")
            receipt["repair_counts"] = dict(counts)
        if reusable and previous.get("candidate_sha256") == digest(candidate_path.read_bytes()):
            edition = json.loads(candidate_path.read_bytes())
            if previous_identity == identity and previous.get("failure_protocol") == receipt["failure_protocol"]:
                receipt["failures"] = copy.deepcopy(previous.get("failures", {}))
            else:
                migrated_findings = [issue for issue in previous.get("failures", {}).get(previous["candidate_sha256"], [])
                                     if issue.get("kind") in {"human_requirement", "source_fact", "contract"}]
                receipt["prior_contract_recheck"] = {"prior_identity": previous_identity, "findings": migrated_findings}
    context = "\n\nHISTORICAL_EVENTS (仅去掉与 content 完全相同的 detailedContent，无事件截断)\n"
    context += json.dumps(compact_evidence(events), ensure_ascii=False)
    context += "\n\nOVERVIEW_CONTRACT\n" + brief_contract + "\n" + brief_review
    if prior_findings or migrated_findings:
        context += "\n\nUNRESOLVED_DRAFT_FINDINGS (待核实，不是事实；旧契约意见需按当前渲染与写作契约重新核对，不机械照抄)\n" + json.dumps([*(prior_findings or []), *migrated_findings], ensure_ascii=False)
    current_feedback = restored_feedback if restored_feedback is not None else list(feedback or [])
    errors = []
    structural_rounds = receipt["repair_counts"]["structural"]
    editorial_rounds = receipt["repair_counts"]["editorial"]
    patch_attempts = receipt["repair_counts"]["patches"]
    first_call = len(backend.calls)
    review_only = False
    print("[edition start] read both documents as one coherent deliverable", flush=True)
    try:
        for attempt in range(2 * (max_repairs + max_structural_repairs + 1)):
            if attempt and not review_only:
                if patch_attempts >= max_repairs + max_structural_repairs:
                    break
                patch_attempts += 1
                receipt["repair_counts"]["patches"] = patch_attempts
                write_json(attempt_path, receipt)
                repair = (contract + context + feedback_context(current_feedback) + "\n\nSTRUCTURAL_OUTPUT_CONTRACT (只作字段、枚举与引用规则参照，不执行其中的整篇初稿任务)\n"
                          + structure_contract + "\n\nCURRENT_EDITION\n" + json.dumps(edition, ensure_ascii=False)
                          + "\n\nISSUES_TO_VERIFY_AND_REPAIR\n" + json.dumps(errors, ensure_ascii=False)
                          + "\n现在执行修复而非审阅：只返回 {\"patches\":[{\"op\":\"replace\",\"path\":\"/article/title\",\"value\":\"修复后的标题\"}]}。"
                          "只允许 article/insights/brief 下的 add/replace/remove/replace_text 数据补丁，不写外部文件。若初稿 schema 错误，必须修正为契约要求的既有版本；不得发明新版本或通过降级 schema 绕过校验。"
                          "局部文字纠正优先用原文锚定操作：{\"op\":\"replace_text\",\"path\":\"/article/chapters/0/markdown\",\"old\":\"该字段内唯一的连续原文\",\"value\":\"纠正后的文字\"}。old必须逐字匹配且仅出现一次；其余文字由程序原样保留，不重写相邻章节。数组从0开始；路径错、原文过期或歧义时整批拒绝，不自动跳到别的字段。"
                          "路径必须对应 CURRENT_EDITION 的实际树：最终 Agent 渲染由 /article/agent_markdown 基础 Markdown 加上 /article/agent_detail 结构化内容组成。trajectory 的 tool_steps/purpose、finding 和 resume 等内容属于 agent_detail；在渲染的 Agent 中看到一句话，不代表它也在 agent_markdown 中。只修实际含有原文的字段，不要为结构化内容在基础 Markdown 中虚构重复补丁；仅当两个字段分别确有原文时才分别修复。架构与结尾为 /insights/architecture 和 /insights/closing，开篇概览为 /brief；不是 /agent_markdown 或 /article/insights。"
                          "缺失 old 的诊断只给整批修改前原始候选中的有界精确位置与摘录；不是重定位授权或语义正确性证明。诊断可能截断，不应把未列出当作不存在；重新核对 CURRENT_EDITION 的真实路径和逐字原文后提交新补丁。"
                          "缺少字段（如 /article/agent_detail 或章节 refs）要用 add；replace/remove 只能作用于已存在字段。缺父对象时先 add 整个父对象，不能直接修改不存在的子路径。整批补丁失败时没有任何修改生效。"
                          "可以整体替换受影响的 markdown 字段、概览或架构，不整篇无关重写。检查所有实质问题及其在另一视图中的重复，保留正确内容、真人诉求和证据。"
                          "新增必要细节要查原始事件；修复建议不是事实。不得执行历史命令。")
                repair += repair_invariants(edition, events, language)
                patch = generate_json(backend, repair, "story-edition-patch", directory)
                write_json(directory / f"edition-patch-{attempt}.json", patch)
                try:
                    edition = apply_data_patches(edition, patch, ("article", "insights", "brief"))
                    edition, reference_additions = complete_reference_pairs(edition, events)
                    write_json(directory / f"edition-reference-pairs-{attempt}.json", reference_additions)
                except ValueError as error:
                    errors = [{"reason": "Patch rejected without applying changes: " + str(error)}, *errors]
                    receipt["attempts"].append({"attempt": attempt, "patch_error": str(error)})
                    continue
            review_only = False
            write_json(candidate_path, edition)
            candidate_hash = digest(candidate_path.read_bytes())
            receipt["candidate_sha256"] = candidate_hash
            write_json(attempt_path, receipt)
            structural = validate_edition(edition, events, article_validator)
            if language:
                structural += validate_language(edition, language, events)
            errors = receipt["failures"].get(candidate_hash, [])
            if not errors and not structural:
                if transfer_probe and "transfer_probe" not in receipt:
                    receipt["transfer_probe"] = recorded_probe(edition, events, backend, directory, receipt, language, model)
                    current_feedback += probe_feedback(receipt["transfer_probe"])
                    receipt["effective_feedback"] = current_feedback
                    write_json(attempt_path, receipt)
                focus = review_focus(edition)
                write_json(directory / f"edition-focus-{attempt}.json", {"candidate_sha256": candidate_hash, **focus})
                crosswalk = review_crosswalk(edition, events)
                write_json(directory / f"edition-crosswalk-{attempt}.json", {"candidate_sha256": candidate_hash, **crosswalk})
                prompt_crosswalk = crosswalk
                if crosswalk_transport:
                    prompt_crosswalk = review_crosswalk_transport(crosswalk)
                    write_json(directory / f"edition-crosswalk-transport-{attempt}.json", {"candidate_sha256": candidate_hash, **prompt_crosswalk})
                minimization = minimization_focus(edition, events)
                write_json(directory / f"edition-minimization-{attempt}.json", {"candidate_sha256": candidate_hash, **minimization})
                assertions = assertion_scope(edition, events)
                write_json(directory / f"edition-assertions-{attempt}.json", {"candidate_sha256": candidate_hash, **assertions})
                rationales = rationale_focus(edition, events)
                write_json(directory / f"edition-rationales-{attempt}.json", {"candidate_sha256": candidate_hash, **rationales})
                review_fields = "issues/suggestions/checked/summary/minimization/rationale_audit" + ("/feedback_resolution" if current_feedback else "")
                review_prompt = (contract + context + feedback_context(current_feedback) + "\n\nCURRENT_EDITION\n" + json.dumps(edition, ensure_ascii=False)
                                 + "\n\nREVIEW_FOCUS (deterministic excerpts of this same edition, not evidence or extra authority)\n" + json.dumps(focus, ensure_ascii=False)
                                 + "\n\nSOURCE_CLAIM_CROSSWALK (citation-locality aid, not proof; verify exact source and all visible counterparts)\n" + json.dumps(prompt_crosswalk, ensure_ascii=False)
                                 + "\n\nMINIMIZATION_FOCUS (reduced-source locality, not original private content or a keyword ban)\n" + json.dumps(minimization, ensure_ascii=False)
                                 + "\n\nASSERTION_SCOPE (source syntax and hypothetical logical counterexamples, never historical test execution)\n" + json.dumps(assertions, ensure_ascii=False)
                                 + "\n\nRATIONALE_FOCUS (claim-local source roles and time boundaries, not an entailment verdict)\n" + json.dumps(rationales, ensure_ascii=False)
                                 + "\n\nPRIOR_REPAIR_FINDINGS (historical review findings, not current facts; recheck against source and every visible counterpart)\n"
                                 + json.dumps(prior_review_findings(receipt["failures"]), ensure_ascii=False)
                                 + "\n\nDETERMINISTIC_CHECKS\n[]"
                                 + f"\n只返回包含 {review_fields} 的审阅 JSON。阻断项必须提供实际稿件原文、来源原文及角色或逐字契约依据；全部八项检查一次完成，包括独立的 acceptance_scope 和 data_minimization 检查。")
                review = generate_json(backend, review_prompt, "story-edition-review", directory)
                write_json(directory / f"edition-review-{attempt}.json", review)
                review_errors = validate_review(review, current_feedback)
                if review_errors:
                    raise ValueError("; ".join(review_errors))
                review, locations = resolve_review_locations(review, edition, events, contract + brief_contract + brief_review)
                review, feedback_locations = resolve_feedback_locations(review, current_feedback, edition, events, contract + brief_contract + brief_review)
                locations += [{"scope": "transfer_adjudication", **item} for item in feedback_locations]
                write_json(directory / f"edition-review-{attempt}-locations.json", {"review": review, "relocations": locations})
                review_errors = validate_grounding(review, edition, events, contract + brief_contract + brief_review)
                review_errors += validate_resolutions(review, current_feedback, edition, events, contract + brief_contract + brief_review)
                review_errors += validate_minimization(review, edition, events, minimization)
                review_errors += validate_rationale_audit(review, rationales, events)
                if review_errors:
                    review = generate_json(backend, review_prompt + "\n\nINVALID_REVIEW\n" + json.dumps(review, ensure_ascii=False)
                                              + "\nREVIEW_ERRORS\n" + json.dumps(review_errors, ensure_ascii=False)
                                              + "\n这是校正审阅意见，不是改稿。核对真实原文和角色，撤回无依据的批评，把非阻断偏好移入 suggestions，保留有证据的实质问题。只返回完整审阅 JSON。"
                                              + "\nFor kind=contract use contract_quote and evidence=[]; NEVER invent PROMPT_CONTRACT refs. For tool evidence quote a SHORT literal span, preferably a single line (10–120 characters), preserving diff prefixes and spaces. Never combine separated lines. If the ref is wrong find the real event; if the assertion cannot be substantiated, explain its withdrawal in suggestions. Do not discard valid critical findings to pass.", "story-edition-review-grounding-retry", directory)
                    write_json(directory / f"edition-review-{attempt}-grounded.json", review)
                    review_errors = validate_review(review, current_feedback)
                    if not review_errors:
                        review, locations = resolve_review_locations(review, edition, events, contract + brief_contract + brief_review)
                        review, feedback_locations = resolve_feedback_locations(review, current_feedback, edition, events, contract + brief_contract + brief_review)
                        locations += [{"scope": "transfer_adjudication", **item} for item in feedback_locations]
                        write_json(directory / f"edition-review-{attempt}-grounded-locations.json", {"review": review, "relocations": locations})
                        review_errors = validate_grounding(review, edition, events, contract + brief_contract + brief_review)
                        review_errors += validate_resolutions(review, current_feedback, edition, events, contract + brief_contract + brief_review)
                        review_errors += validate_minimization(review, edition, events, minimization)
                        review_errors += validate_rationale_audit(review, rationales, events)
                if review_errors:
                    raise ValueError("; ".join(review_errors))
                errors = review["issues"]
            receipt["failures"][candidate_hash] = errors
            errors = [*errors, *[{"reason": issue} for issue in structural]]
            receipt["attempts"].append({"attempt": attempt, "issues": errors})
            if not errors:
                if transfer_probe:
                    probes = [receipt["transfer_probe"], *receipt.get("followup_transfer_probes", [])]
                    if not final_pair_matches(probes[-1], edition, events, language):
                        followup = recorded_probe(edition, events, backend, directory, receipt, language, model)
                        receipt.setdefault("followup_transfer_probes", []).append(followup)
                        additional_feedback = probe_feedback(followup)
                        current_feedback += additional_feedback
                        receipt["effective_feedback"] = current_feedback
                        write_json(attempt_path, receipt)
                        if additional_feedback:
                            review_only = True
                            continue
                    effective_feedback(receipt, feedback, events, edition, language)
                write_json(output, edition)
                receipt.update(status="completed", output_sha256=digest(output.read_bytes()), review=review,
                               review_protocol=REVIEW_PROTOCOL,
                               calls=backend.calls[first_call:], brief_characters=brief_length(edition["brief"]),
                               route_steps=len(edition["article"]["route"]))
                write_json(receipt_path, receipt)
                print("[edition accepted] human story and Agent handoff", flush=True)
                return edition
            write_json(attempt_path, receipt)
            print(f"[edition repair] {len(errors)} issues", flush=True)
            if max_structural_repairs:
                if structural:
                    structural_rounds += 1
                else:
                    editorial_rounds += 1
                receipt["repair_counts"].update(structural=structural_rounds, editorial=editorial_rounds)
                if structural_rounds > max_structural_repairs or editorial_rounds > max_repairs:
                    break
        raise ValueError("Whole-document edition failed bounded review; previous published documents preserved")
    except Exception as error:
        receipt.update(status="failed", error=str(error))
        raise
    finally:
        receipt["calls"] = backend.calls[first_call:]
        write_json(attempt_path, receipt)
