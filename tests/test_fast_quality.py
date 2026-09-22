import copy
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from offline_provider import guard_offline_test
from session_spec.backend import ModelResponseError
from session_spec.delivery import deliver
from session_spec.draft_recovery import create_recovery, recovery_snapshot
from session_spec.fast_quality import CHECKS, CONTRACT, LEGACY_SCHEMA, SCHEMA, DraftPrivacyBackend, FastQualityBackend, QualityReviewFailure, review_source_quality, selected_reader_text, validate_quality, validate_quality_receipt
from session_spec.privacy_presentation import present_review
from session_spec.reduction import scan_session, load_review
from session_spec.reduction_semantic import semantic_review
from session_spec.storage import file_hash, write_json


def quality():
    return {"schema": SCHEMA, "verdict": "pass", "checked": [{"category": category, "note": "PATH remains unverified."} for category in CHECKS], "attention": [], "issues": []}


class Backend:
    def __init__(self, response):
        self.response = response
        self.calls = [{"label": "draft"}]
        self.prompts = []

    def generate(self, prompt, label):
        self.calls.append({"label": label})
        self.prompts.append(prompt)
        return copy.deepcopy(self.response)


class ResponseSequenceBackend(Backend):
    def generate(self, prompt, label):
        self.calls.append({"label": label})
        self.prompts.append(prompt)
        response = self.response.pop(0)
        if isinstance(response, Exception):
            raise response
        return copy.deepcopy(response)


class FastQualityTests(unittest.TestCase):
    def test_source_quality_prompt_cannot_request_privacy_findings(self):
        example = next(line for line in CONTRACT.splitlines() if line.startswith('{"quality":'))
        response = json.loads(example.rstrip('.'))
        self.assertEqual(set(response), {"quality"})
        self.assertEqual(set(response["quality"]), {"schema", "verdict", "checked", "attention", "issues"})
        self.assertEqual(response["quality"]["schema"], SCHEMA)
        self.assertIn("topics may overlap in time", CONTRACT)
        self.assertIn("PRIVATE, PRE-REDACTION", CONTRACT)
        self.assertIn("not a source-quality failure", CONTRACT)
        self.assertIn("old content embedded in a later patch is NOT a later readback", CONTRACT)
        self.assertIn("neither this original-session packet nor your review output", CONTRACT)
        self.assertNotIn("SOURCE SLOTS", CONTRACT)

    def setUp(self):
        guard_offline_test(self)
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.artifact = self.root / "source/events.jsonl"
        self.artifact.parent.mkdir()
        self.artifact.write_text(json.dumps({"type": "artifact.abstracted", "data": {"agent": "PATH remains unverified. I am embarrassed."}}) + "\n", encoding="utf-8")
        self.support = self.root / "story/_support"
        self.support.mkdir(parents=True)
        self.events = [{"ref": "E000001", "type": "user.message", "text": "Check PATH; it is unverified.", "human_input": "Check PATH; it is unverified."}]
        write_json(self.support / "source.json", {"source_sha256": "fixture-source"})
        write_json(self.support / "input.json", self.events)
        write_json(self.support / "story-report.json", {"status": "structurally_valid_unreviewed"})

    def tearDown(self):
        self.temporary.cleanup()

    def wrapper(self, response):
        self.backend = Backend(response)
        return FastQualityBackend(self.backend, self.events, self.artifact, self.support / "story-report.json", self.support / "fast-quality.json")

    def attention_fixture(self, readers=("human", "agent")):
        self.events.append({"ref": "E000016", "type": "assistant.message", "origin": "root",
                            "text": "Historical TestMain: if !available { print(\"skipping E2E tests\"); os.Exit(0) }; m.Run()"})
        write_json(self.support / "input.json", self.events)
        human = "Recorded tests can skip with exit zero; require non-skipped execution logs & counts."
        agent = "Confirm actual non-skipped test execution: a green status alone is insufficient."
        surface = {reader: ({} if reader == "human" else agent) for reader in readers}
        surface["evidence"] = "Source-only excerpt: skipping E2E tests with os.Exit(0)."
        self.artifact.write_text(json.dumps({"type": "artifact.abstracted", "data": surface}) + "\n", encoding="utf-8")
        (self.support.parent / "human-spec.html").write_text(
            '<html><head><script>SECRET_SCRIPT_ONLY</script></head><body><main><p>Recorded tests can '
            '<em>skip</em> with exit zero; require non-skipped execution logs &amp;\n counts.</p>'
            '<dialog><textarea>AGENT_DIALOG_ONLY</textarea></dialog><p hidden>HIDDEN_ONLY</p></main></body></html>', encoding="utf-8")
        entry = {"ref": "E000016", "category": "acceptance_or_failure_condition", "disposition": "retained",
                 "note": "The recorded zero-exit skip makes a non-skipped execution check necessary.",
                 "coverage": [{"reader": reader, "quote": human if reader == "human" else agent} for reader in readers]}
        checked = quality()
        checked["attention"] = [entry]
        return checked

    def test_generic_five_check_pass_cannot_ignore_zero_exit_attention_or_exceed_budget(self):
        self.attention_fixture()
        wrapper = self.wrapper({"quality": quality()})
        self.backend.calls.append({"label": "structural-repair"})
        with self.assertRaisesRegex(QualityReviewFailure, "within the model-call budget"):
            review_source_quality(wrapper, [])
        self.assertEqual(len(self.backend.calls), 3)
        self.assertEqual(len(wrapper.calls), 2)  # repair plus the single remaining review call
        self.assertFalse((self.support / "fast-quality.json").exists())
        self.assertIn('"ref": "E000016"', self.backend.prompts[0])
        self.assertIn("os.Exit(0)", self.backend.prompts[0])
        self.assertEqual(len(list((self.support / "fast-quality-attempts").glob("*.json"))), 1)

    def test_material_missing_attention_fails_once_with_actual_source_support(self):
        checked = self.attention_fixture()
        checked["attention"][0].update(disposition="missing", coverage=[], note="The Agent draft does not retain the recorded skip boundary.")
        checked.update(verdict="fail", issues=[{"reason": "Acceptance does not establish non-skipped execution.",
                                                 "refs": ["E000016"], "quote": "skipping E2E tests"}])
        wrapper = self.wrapper({"quality": checked})
        with self.assertRaises(QualityReviewFailure):
            review_source_quality(wrapper, [])
        self.assertEqual(len(wrapper.calls), 1)
        self.assertEqual(json.loads((self.support / "fast-quality.json").read_bytes())["result"]["verdict"], "fail")
        checked["issues"] = [{"reason": "Unrelated PATH uncertainty.", "refs": ["E000001"], "quote": "it is unverified"}]
        with self.assertRaisesRegex(ValueError, "source-grounded failing issue"):
            validate_quality(checked, self.events)

    def test_retained_attention_quotes_each_selected_reader_with_html_normalization(self):
        checked = self.attention_fixture()
        readers = selected_reader_text(self.artifact, self.support / "story-report.json")
        self.assertNotIn("AGENT_DIALOG_ONLY", readers["human"])
        self.assertNotIn("HIDDEN_ONLY", readers["human"])
        self.assertNotIn("SECRET_SCRIPT_ONLY", readers["human"])
        original = copy.deepcopy((checked, self.events))
        self.assertEqual(validate_quality(checked, self.events, readers), checked)
        for selected in ({"human": readers["human"]}, {"agent": readers["agent"]}):
            subset = copy.deepcopy(checked)
            subset["attention"][0]["coverage"] = [item for item in subset["attention"][0]["coverage"] if item["reader"] in selected]
            self.assertEqual(validate_quality(subset, self.events, selected), subset)
        self.assertEqual((checked, self.events), original)
        wrapper = self.wrapper({"quality": checked})
        self.assertEqual(wrapper.generate("synthetic", "test"), checked)
        manifest = {"artifact_sha256": file_hash(self.artifact), "quality_review_sha256": file_hash(self.support / "fast-quality.json"), "quality_profile": SCHEMA}
        validate_quality_receipt(self.root, manifest)

    def test_evidence_or_hidden_companion_cannot_substitute_for_reader_prose(self):
        checked = self.attention_fixture()
        readers = selected_reader_text(self.artifact, self.support / "story-report.json")
        for quote in ("Source-only excerpt: skipping E2E tests with os.Exit(0).", "AGENT_DIALOG_ONLY", "HIDDEN_ONLY", "Invented acceptance statement", "&nbsp;", "&#x20;"):
            response = copy.deepcopy(checked)
            response["attention"][0]["coverage"][0]["quote"] = quote
            with self.subTest(quote=quote), self.assertRaisesRegex(ValueError, "actual selected-reader quotations"):
                validate_quality(response, self.events, readers)
        checked["attention"][0]["coverage"].pop()
        with self.assertRaisesRegex(ValueError, "every selected reader"):
            validate_quality(checked, self.events, readers)

    def test_contextual_candidate_exclusion_requires_reason_but_not_invented_reader_content(self):
        self.events.append({"ref": "E000002", "type": "assistant.message", "text": "Unrelated tutorial: the old no-op example is not the selected workflow."})
        checked = quality()
        checked["attention"] = [{"ref": "E000002", "category": "acceptance_or_failure_condition", "disposition": "not_material",
                                  "note": "The complete event identifies a separate tutorial example, not behavior of the task's workflow.", "coverage": []}]
        wrapper = self.wrapper({"quality": checked})
        self.assertEqual(wrapper.generate("synthetic", "test"), checked)
        for change in ({"note": ""}, {"ref": "E000003"}, {"coverage": [{"reader": "agent", "quote": "PATH remains unverified."}]}):
            invalid = copy.deepcopy(checked)
            invalid["attention"][0].update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_quality(invalid, self.events, {"agent": "PATH remains unverified."})

    def test_all_distinct_attention_candidates_need_accounting_including_security_warning(self):
        checked = self.attention_fixture()
        self.events.append({"ref": "E000023", "type": "assistant.message", "text": "Historical warning: rotate the exposed test credential; remediation is unverified."})
        checked["attention"].append(copy.deepcopy(checked["attention"][0]))
        with self.assertRaisesRegex(ValueError, "Invalid source attention assessment"):
            validate_quality(checked, self.events, selected_reader_text(self.artifact, self.support / "story-report.json"))

    def test_legacy_receipts_remain_explicitly_legacy_but_new_review_rejects_old_schema(self):
        self.attention_fixture()
        legacy = quality()
        legacy["schema"] = LEGACY_SCHEMA
        legacy.pop("attention")
        receipt = self.support / "fast-quality.json"
        record = {"schema": LEGACY_SCHEMA, "result": legacy, "source_sha256": file_hash(self.support / "source.json"),
                  "artifact_sha256": file_hash(self.artifact), "story_report_sha256": file_hash(self.support / "story-report.json")}
        write_json(receipt, record)
        manifest = {"artifact_sha256": file_hash(self.artifact), "quality_review_sha256": file_hash(receipt), "quality_profile": LEGACY_SCHEMA}
        validate_quality_receipt(self.root, manifest)
        with self.assertRaises(ValueError):
            validate_quality(legacy, self.events)
        manifest["quality_profile"] = SCHEMA
        with self.assertRaisesRegex(ValueError, "receipt changed"):
            validate_quality_receipt(self.root, manifest)

    def test_source_quality_and_privacy_receive_disjoint_inputs_and_response_schemas(self):
        directory = self.root / "review"
        scan_session(self.artifact, self.root / "home", directory, audience="local")
        self.events[0]["text"] += " ORIGINAL_ONLY_NOT_IN_DRAFT"
        checked = quality()
        checked["checked"][0]["note"] = "QUALITY_OUTPUT_NOT_FOR_PRIVACY"
        wrapper = self.wrapper({"quality": checked})
        surface = [json.loads(self.artifact.read_text(encoding="utf-8"))]
        review_source_quality(wrapper, surface)
        self.backend.response = {"findings": [{"slot": "S1", "quote": "I am embarrassed.", "category": "reputation", "necessity": "unnecessary", "reason": "Personal aside unrelated to the technical PATH limitation.", "alternative": "", "related": []}], "limitations": []}
        result = semantic_review(directory, consent=True, backend=DraftPrivacyBackend(self.backend), max_calls=1, repair_attempts=0)
        self.assertEqual(len(self.backend.calls), 3)
        self.assertEqual(result["semantic"]["calls"], 1)
        self.assertEqual(result["semantic"]["status"], "reviewed")
        self.assertIn("FULL_SOURCE_EVENTS", self.backend.prompts[0])
        self.assertNotIn("SOURCE SLOTS", self.backend.prompts[0])
        self.assertIn("ORIGINAL_ONLY_NOT_IN_DRAFT", self.backend.prompts[0])
        self.assertIn("SOURCE SLOTS", self.backend.prompts[1])
        self.assertNotIn("FULL_SOURCE_EVENTS", self.backend.prompts[1])
        self.assertNotIn("SELECTED_READER_TEXT", self.backend.prompts[1])
        self.assertNotIn("ORIGINAL_ONLY_NOT_IN_DRAFT", self.backend.prompts[1])
        self.assertNotIn("QUALITY_OUTPUT_NOT_FOR_PRIVACY", self.backend.prompts[1])
        review, baseline = load_review(directory)
        visible = present_review(review, baseline)
        self.assertEqual(visible["findings"][0]["recommended"], "remove")
        self.assertNotIn("decisions.json", [path.name for path in directory.iterdir()])

    def test_quality_failure_is_not_retried_or_published_as_reviewed(self):
        directory = self.root / "review"
        scan_session(self.artifact, self.root / "home", directory, audience="local")
        rejected = quality()
        rejected.update(verdict="fail", issues=[{"reason": "The draft omits uncertainty.", "refs": ["E000001"], "quote": "it is unverified"}])
        response = {"quality": rejected}
        wrapper = self.wrapper(response)
        with self.assertRaises(QualityReviewFailure) as caught:
            review_source_quality(wrapper, [])
        self.assertEqual(len(self.backend.calls), 2)
        self.assertNotEqual(load_review(directory)[0].get("semantic", {}).get("status"), "reviewed")
        attempts = list((self.support / "fast-quality-attempts").glob("*.json"))
        self.assertEqual(len(attempts), 1)
        self.assertEqual(json.loads(attempts[0].read_bytes())["response"], response)
        self.assertNotIn("PRIVATE_FINDING_CANARY", str(caught.exception))
        self.assertEqual(json.loads((self.support / "fast-quality.json").read_bytes())["result"], rejected)

    def test_unrelated_human_citation_is_visible_to_quality_with_original_rejection_separate(self):
        self.events = [{"ref": "E000001", "type": "user.message", "origin": "root",
                        "human_input": "Is the matrix parallel?", "text": "Is the matrix parallel?"},
                       {"ref": "E000002", "type": "assistant.message", "origin": "root",
                        "text": "Archived tool report: the edit was rejected."}]
        surface = [{"type": "artifact.abstracted", "data": {"human": {"brief": {"constraints": [
            {"kind": "requirement", "text": "Never edit without approval.", "refs": ["E000001"]}]}}}}]
        original = copy.deepcopy((surface, self.events))
        rejected = quality()
        rejected.update(verdict="fail", issues=[{"reason": "The cited question does not authorize the claimed blanket restriction.",
                                                  "refs": ["E000001"], "quote": "Is the matrix parallel?"}])
        rejected["attention"] = [{"ref": "E000002", "category": "acceptance_or_failure_condition", "disposition": "not_material",
                                  "note": "This related historical rejection is distinct from the unsupported human attribution identified in the issue.", "coverage": []}]
        wrapper = self.wrapper({"quality": rejected})
        with self.assertRaises(QualityReviewFailure):
            review_source_quality(wrapper, surface)
        prompt = self.backend.prompts[0]
        claims = json.loads(prompt.split("BRIEF_CLAIM_SOURCE_ALIGNMENT:\n", 1)[1].split("\nSELECTED_DOCUMENTS:", 1)[0])
        self.assertEqual(claims[0]["text"], "Never edit without approval.")
        self.assertEqual([source["ref"] for source in claims[0]["cited_sources"]], ["E000001"])
        self.assertEqual(claims[0]["cited_sources"][0]["human_input_excerpt"], "Is the matrix parallel?")
        self.assertIn("Archived tool report: the edit was rejected.", prompt)
        self.assertEqual((surface, self.events), original)
        self.assertEqual(len(wrapper.calls), 1)
        self.assertEqual(json.loads((self.support / "fast-quality.json").read_bytes())["result"]["verdict"], "fail")

    def test_invalid_quality_keeps_each_raw_response_without_partial_privacy_publication(self):
        directory = self.root / "review"
        scan_session(self.artifact, self.root / "home", directory, audience="local")
        original = (directory / "review.json").read_bytes()
        response = {"quality": {"schema": SCHEMA}, "findings": [{"reason": "PRIVATE_RAW_FAILURE", "quote": "Exact\r\n原文"}], "limitations": []}
        wrapper = self.wrapper(response)
        with self.assertRaisesRegex(QualityReviewFailure, "no quality approval") as caught:
            review_source_quality(wrapper, [])
        attempts = list((self.support / "fast-quality-attempts").glob("*.json"))
        self.assertEqual(len(attempts), 2)
        self.assertEqual(len(self.backend.calls), 3)
        for attempt in attempts:
            self.assertEqual(json.loads(attempt.read_bytes())["response"], response)
            self.assertNotIn(b"\r\n", attempt.read_bytes())
        self.assertFalse((self.support / "fast-quality.json").exists())
        self.assertEqual((directory / "review.json").read_bytes(), original)
        self.assertNotIn("PRIVATE_RAW_FAILURE", str(caught.exception))
        self.assertNotIn("PRIVATE_RAW_FAILURE", str(caught.exception.__cause__))

    def test_nonobject_parsed_response_is_preserved_without_public_disclosure(self):
        response = ["PRIVATE_NONOBJECT_RESPONSE", {"original": "原文\r\nexact"}]
        wrapper = self.wrapper(response)
        with self.assertRaisesRegex(ValueError, "must contain only quality") as caught:
            wrapper.generate("synthetic", "test")
        attempts = list((self.support / "fast-quality-attempts").glob("*.json"))
        self.assertEqual(len(attempts), 1)
        self.assertEqual(json.loads(attempts[0].read_bytes())["response"], response)
        self.assertNotIn("PRIVATE_NONOBJECT_RESPONSE", str(caught.exception))
        self.assertFalse((self.support / "fast-quality.json").exists())

    def test_malformed_quality_json_is_retained_then_repaired_within_budget(self):
        raw = "PRIVATE_INVALID_JSON\r\n{\"quality\": 原文"
        backend = ResponseSequenceBackend([ModelResponseError("Synthetic private parse detail", raw), {"quality": quality()}])
        wrapper = FastQualityBackend(backend, self.events, self.artifact, self.support / "story-report.json", self.support / "fast-quality.json")
        self.assertEqual(review_source_quality(wrapper, []), quality())
        self.assertEqual(len(backend.calls), 3)
        attempts = [json.loads(path.read_bytes()) for path in (self.support / "fast-quality-attempts").glob("*.json")]
        self.assertEqual(len(attempts), 2)
        failed = next(attempt for attempt in attempts if "json_error" in attempt)
        self.assertEqual(failed["response"], raw)
        self.assertEqual(failed["json_error"], "Synthetic private parse detail")
        self.assertIn("not valid JSON", backend.prompts[1])
        self.assertNotIn(raw, backend.prompts[1])
        self.assertNotIn("Synthetic private parse detail", backend.prompts[1])
        self.assertEqual(json.loads((self.support / "fast-quality.json").read_bytes())["result"], quality())

    def test_repeated_malformed_quality_json_has_recoverable_failure_without_approval(self):
        raw = "PRIVATE_INVALID_JSON"
        directory = self.root / "review"
        scan_session(self.artifact, self.root / "home", directory, audience="local")
        original = (directory / "review.json").read_bytes()
        backend = ResponseSequenceBackend([ModelResponseError("Synthetic private parse detail", raw), ModelResponseError("Synthetic private parse detail", raw)])
        wrapper = FastQualityBackend(backend, self.events, self.artifact, self.support / "story-report.json", self.support / "fast-quality.json")
        with self.assertRaises(QualityReviewFailure) as caught:
            review_source_quality(wrapper, [])
        self.assertEqual(caught.exception.error_code, "draft_quality_invalid")
        self.assertEqual(len(backend.calls), 3)
        attempts = [json.loads(path.read_bytes()) for path in (self.support / "fast-quality-attempts").glob("*.json")]
        self.assertEqual(len(attempts), 2)
        self.assertTrue(all(attempt["response"] == raw for attempt in attempts))
        self.assertFalse((self.support / "fast-quality.json").exists())
        self.assertEqual((directory / "review.json").read_bytes(), original)
        self.assertNotIn(raw, str(caught.exception))
        self.assertNotIn("Synthetic private parse detail", str(caught.exception))
        job = self.root / "recoverable-job"
        (job / "abstraction/story/_support").mkdir(parents=True)
        write_json(job / "abstraction/story/_support/fast-candidate-0.json", {"article": {"opening": "Synthetic retained draft"}})
        identifier = create_recovery(job, "human", caught.exception.error_code)
        _, snapshot = recovery_snapshot(job, identifier)
        self.assertIn("human-spec.html", snapshot["files"])

    def test_raw_attempt_is_written_before_validation_and_never_overwritten(self):
        response = {"quality": quality()}
        wrapper = self.wrapper(response)

        def rejected(result, events, readers):
            attempts = list((self.support / "fast-quality-attempts").glob("*.json"))
            self.assertEqual(len(attempts), 1)
            self.assertEqual(json.loads(attempts[0].read_bytes())["response"], response)
            raise ValueError("Synthetic safe validation failure")

        with patch("session_spec.fast_quality.uuid.uuid4", return_value=SimpleNamespace(hex="fixed-private-attempt")):
            with patch("session_spec.fast_quality.validate_quality", side_effect=rejected), self.assertRaisesRegex(ValueError, "safe validation"):
                wrapper.generate("synthetic", "test")
            saved = self.support / "fast-quality-attempts/fixed-private-attempt.json"
            original = saved.read_bytes()
            with patch("session_spec.fast_quality.validate_quality", side_effect=AssertionError("No approval after failed retention")), \
                    self.assertRaisesRegex(RuntimeError, "could not be retained") as caught:
                wrapper.generate("synthetic", "test-again")
            self.assertEqual(saved.read_bytes(), original)
            self.assertNotIn(str(self.root), str(caught.exception))

    def test_success_receipt_binding_and_delivery_exclude_private_diagnostics(self):
        checked = quality()
        checked["checked"][0]["note"] = "PRIVATE_DIAGNOSTIC_ONLY"
        response = {"quality": checked}
        wrapper = self.wrapper(response)
        self.assertEqual(wrapper.generate("synthetic", "test"), checked)
        receipt = self.support / "fast-quality.json"
        expected = {"schema": SCHEMA, "source_sha256": file_hash(self.support / "source.json"),
                    "artifact_sha256": file_hash(self.artifact), "story_report_sha256": file_hash(self.support / "story-report.json"),
                    "result": checked, "calls": wrapper.calls,
                    "scope": "Single source-grounded model assessment; no transfer benchmark or independent task execution."}
        self.assertEqual(json.loads(receipt.read_bytes()), expected)
        manifest = {"artifact_sha256": file_hash(self.artifact), "quality_review_sha256": file_hash(receipt)}
        validate_quality_receipt(self.root, manifest)
        names = {"human-spec.html", "agent-spec.md", "evidence.md"}
        for name in names:
            (self.support.parent / name).write_bytes(b"Synthetic selected reader content")
        selection = {"readers": "both", "destination": "local"}
        (self.root / "reduced").mkdir()
        write_json(self.root / "reduced/reduction.json", {"audience": "local", "preferences": selection, "review_id": "synthetic-delivery-fixture"})
        result = deliver(self.root, selection)
        self.assertEqual(set(result["files"]), names)
        for path in (self.root / "deliverables").iterdir():
            self.assertNotIn(b"PRIVATE_DIAGNOSTIC_ONLY", path.read_bytes())
        with zipfile.ZipFile(self.root / "deliverables.zip") as archive:
            self.assertEqual(set(archive.namelist()), names)
            for name in names:
                self.assertNotIn(b"PRIVATE_DIAGNOSTIC_ONLY", archive.read(name))
        self.assertEqual(file_hash(receipt), manifest["quality_review_sha256"])

    def test_bound_receipt_detects_artifact_report_and_quality_tampering(self):
        wrapper = self.wrapper({"quality": quality()})
        review_source_quality(wrapper, [{"type": "artifact.abstracted", "data": {"agent": "PATH remains unverified."}}])
        manifest = {"artifact_sha256": file_hash(self.artifact), "quality_review_sha256": file_hash(self.support / "fast-quality.json")}
        validate_quality_receipt(self.root, manifest)
        record = json.loads((self.support / "fast-quality.json").read_bytes())
        record["result"]["checked"][0]["note"] = "tampered"
        write_json(self.support / "fast-quality.json", record)
        with self.assertRaisesRegex(ValueError, "receipt changed"):
            validate_quality_receipt(self.root, manifest)

    def test_missing_checks_conflicting_verdict_and_fabricated_quote_fail(self):
        variants = [quality(), quality(), quality()]
        variants[0]["checked"].pop()
        variants[1]["verdict"] = "fail"
        variants[2].update(verdict="fail", issues=[{"reason": "Missing", "refs": ["E000001"], "quote": "invented quotation"}])
        for result in variants:
            with self.subTest(result=result), self.assertRaises(ValueError):
                validate_quality(result, self.events)


if __name__ == "__main__":
    unittest.main()
