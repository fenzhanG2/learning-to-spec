import copy
import hashlib
import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from session_spec.reduction import CONTEXT_SCOPE, add_finding, apply_review, digest, load_review, recommended_decisions, review_identity, scan_session, transform
from session_spec.reduction_semantic import semantic_review
from session_spec import reduction_semantic as semantic_helpers
from session_spec.storage import write_json
from test_reduction import Backend
import test_reduction as reduction_fixtures


class PrivacyContextScopeTests(unittest.TestCase):
    def setUp(self):
        guard = patch("session_spec.reduction_semantic.CopilotBackend", side_effect=AssertionError("Synthetic tests cannot construct a model backend"))
        guard.start()
        self.addCleanup(guard.stop)

    def fixture(self, root, messages, custom=None):
        source = root / "source" / "events.jsonl"
        source.parent.mkdir()
        events = [{"id": str(index), "type": "user.message", "data": {"content": value}}
                  if isinstance(value, str) else value for index, value in enumerate(messages)]
        source.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")
        return scan_session(str(source), root / "unused-home", root / "review", audience="root", custom=custom)

    def choices(self, review, action="keep"):
        return {"review_id": review["review_id"], "audience": review["audience"],
                "choices": {finding["id"]: {"action": action} for finding in review["findings"]}}

    def proposal(self, slot, necessity, reason):
        return {"slot": slot, "quote": "fixture-affiliation", "category": "inference", "necessity": necessity,
                "reason": reason, "alternative": "", "related": []}

    def test_distinct_context_choices_and_metadata_are_order_independent(self):
        proposals = [self.proposal("S1", "unnecessary", "Private aside in the first context"),
                     self.proposal("S2", "necessary", "Literal fixture identifier needed in this technical context")]
        results = []
        for ordered in (proposals, list(reversed(proposals))):
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                reduction_fixtures.ReductionTests().fixture(root, ["My invented detail: fixture-affiliation", "Preserve the literal fixture-affiliation for the regression."])
                review = semantic_review(root / "review", consent=True, backend=Backend([{"findings": ordered}]))
                semantic = [item for item in review["findings"] if "copilot" in item["detectors"]]
                self.assertEqual(len(semantic), 2)
                self.assertEqual(len({item["id"] for item in semantic}), 2)
                choices = recommended_decisions(review)
                self.assertEqual(choices["choices"], {})
                for finding in semantic:
                    choices["choices"][finding["id"]] = {"action": "keep" if finding["necessity"] == "necessary" else "remove"}
                apply_review(root / "review", choices, root / "reduced")
                events = [json.loads(line) for line in (root / "reduced/events.jsonl").read_text(encoding="utf-8").splitlines()]
                self.assertNotIn("fixture-affiliation", events[0]["data"]["content"])
                self.assertIn("fixture-affiliation", events[1]["data"]["content"])
                results.append(sorted(semantic, key=lambda item: item["id"]))
        self.assertEqual(results[0], results[1])

    def test_same_context_conflicts_preserve_both_assessments(self):
        findings_by_order = []
        for necessities in (("necessary", "unnecessary"), ("unnecessary", "necessary")):
            findings = {}
            for necessity in necessities:
                add_finding(findings, "fixture-affiliation", (0, "data", "content"), 0, 19, "inference", detector="copilot",
                            reason=necessity + " assessment", necessity=necessity, alternative=necessity, related=["S2"], context_scoped=True)
            finding = next(iter(findings.values()))
            self.assertEqual(finding["necessity"], "uncertain")
            self.assertEqual(finding["alternative"], "")
            self.assertIsNone(finding["recommended"])
            self.assertEqual(len(finding["assessments"]), 2)
            self.assertIn("Conflicting", finding["reason"])
            findings_by_order.append(finding)
        self.assertEqual(*findings_by_order)

    def test_necessity_conflict_clears_even_a_shared_rewrite_suggestion(self):
        findings = {}
        for necessity in ("necessary", "unnecessary"):
            add_finding(findings, "fixture-affiliation", (0, "data", "content"), 0, 19, "inference", detector="copilot",
                        reason=necessity + " assessment", necessity=necessity, alternative="same proposed rewrite", context_scoped=True)
        finding = next(iter(findings.values()))
        self.assertEqual(finding["necessity"], "uncertain")
        self.assertEqual(finding["alternative"], "")
        self.assertEqual([item["alternative"] for item in finding["assessments"]], ["same proposed rewrite", "same proposed rewrite"])
        self.assertIsNone(finding["recommended"])

    def test_local_group_and_existing_occurrences_are_not_relabelled(self):
        findings = {}
        text = "Contact fixture@example.invalid"
        start = text.index("fixture")
        add_finding(findings, text, (0, "data", "content"), start, len(text), "identifier")
        add_finding(findings, "Again fixture@example.invalid", (1, "data", "content"), 6, 29, "identifier")
        local = copy.deepcopy(next(iter(findings.values())))
        add_finding(findings, text, (0, "data", "content"), start, len(text), "identifier", detector="copilot",
                    reason="Check this context", necessity="necessary", context_scoped=True)
        self.assertEqual(len(findings), 2)
        self.assertEqual(findings[local["id"]], local)
        semantic = next(item for item in findings.values() if "copilot" in item["detectors"])
        self.assertEqual(semantic["scope"]["field_sha256"], digest(text))
        self.assertEqual(len(semantic["occurrences"]), 1)
        self.assertIsNone(semantic["recommended"])

    def test_promotion_partitions_local_groups_and_preserves_bound_parent_and_choices(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = self.fixture(root, ["Private fixture@example.invalid", "Keep fixture fixture@example.invalid"])
            original_bytes = (root / "review/review.json").read_bytes()
            old_choices = self.choices(original)
            write_json(root / "review/decisions.json", old_choices)
            choice_bytes = (root / "review/decisions.json").read_bytes()
            proposals = [{"slot": slot, "quote": "[ENTITY_1]", "category": category, "necessity": necessity,
                          "reason": "Invented context-specific assessment", "alternative": "", "related": []}
                         for slot, category, necessity in (("S1", "identifier", "unnecessary"), ("S2", "inference", "necessary"))]
            newer = semantic_review(root / "review", consent=True, backend=Backend([{"findings": proposals}]))
            self.assertEqual(newer["schema"], "privacy-review/v2")
            self.assertEqual(newer["semantic"]["finding_scope"], CONTEXT_SCOPE)
            self.assertEqual(len(newer["findings"]), 2)
            self.assertEqual(recommended_decisions(newer)["choices"], {})
            self.assertNotEqual(newer["review_id"], original["review_id"])
            self.assertEqual((root / "review/decisions.json").read_bytes(), choice_bytes)
            parent_hash = hashlib.sha256(original_bytes).hexdigest()
            self.assertEqual(newer["parent_review"], {"review_id": original["review_id"], "sha256": parent_hash})
            self.assertEqual((root / "review/audit" / ("review-" + parent_hash + ".json")).read_bytes(), original_bytes)
            review, baseline = load_review(root / "review")
            with self.assertRaisesRegex(ValueError, "match this review"):
                transform(review, baseline, old_choices)
            decisions = self.choices(review)
            for finding in review["findings"]:
                self.assertEqual(len(finding["local_judgments"]), 1)
                local = finding["local_judgments"][0]
                self.assertEqual(local["source_finding_id"], original["findings"][0]["id"])
                for key in ("reason", "necessity", "recommended", "alternative", "related", "detectors"):
                    self.assertEqual(local[key], original["findings"][0][key])
                self.assertEqual(len(finding["assessments"]), 1)
                if finding["occurrences"][0]["path"][0] == 0:
                    decisions["choices"][finding["id"]] = {"action": "remove"}
                else:
                    self.assertEqual(finding["categories"], ["identifier", "inference"])
            result, _ = transform(review, baseline, decisions)
            self.assertNotIn("fixture@example.invalid", result[0]["data"]["content"])
            self.assertEqual(result[1]["data"]["content"], "Keep fixture fixture@example.invalid")

    def test_identical_fields_group_and_pseudonyms_stay_consistent_across_contexts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            text = "fixture@example.invalid plus fixture@example.invalid"
            self.fixture(root, [text, "Again fixture@example.invalid", {"id": "tool", "type": "tool.execution_complete", "data": {"result": {"content": text}, "success": False}}])
            review = semantic_review(root / "review", consent=True, backend=Backend([{"findings": []}]))
            self.assertEqual(sorted(len(item["occurrences"]) for item in review["findings"]), [1, 4])
            _, baseline = load_review(root / "review")
            decisions = self.choices(review, "pseudonymize")
            result, operations = transform(review, baseline, decisions)
            self.assertEqual(json.dumps(result).count("[ENTITY_1]"), 5)
            self.assertEqual(operations, {"pseudonymize": 5})
            self.assertFalse(result[2]["data"]["success"])
            reordered = copy.deepcopy(review)
            reordered["findings"].reverse()
            reordered["review_id"] = review_identity(reordered)
            self.assertEqual(transform(reordered, baseline, self.choices(reordered, "pseudonymize"))[0], result)

    def test_custom_global_is_retained_and_exact_edit_collisions_are_checked(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = self.fixture(root, ["fixture-affiliation", "Keep fixture-affiliation"], custom=["fixture-affiliation"])
            review = semantic_review(root / "review", consent=True, backend=Backend([{"findings": [self.proposal("S1", "unnecessary", "Synthetic aside")]}]))
            custom = next(item for item in review["findings"] if item["category"] == "custom")
            scoped = next(item for item in review["findings"] if "scope" in item)
            self.assertEqual(custom, original["findings"][0])
            _, baseline = load_review(root / "review")
            decisions = self.choices(review)
            decisions["choices"][scoped["id"]] = {"action": "remove"}
            with self.assertRaisesRegex(ValueError, "overlaps"):
                transform(review, baseline, decisions)
            for first, second in (("pseudonymize", "generalize"), ("generalize", "generalize")):
                decisions["choices"][custom["id"]] = {"action": first, "replacement": "first neutral description"}
                decisions["choices"][scoped["id"]] = {"action": second, "replacement": "second neutral description"}
                with self.assertRaisesRegex(ValueError, "Incompatible"):
                    transform(review, baseline, decisions)
            for action in ("remove", "pseudonymize", "generalize"):
                decisions = self.choices(review, action)
                for choice in decisions["choices"].values():
                    choice["replacement"] = "shared neutral description"
                result, operations = transform(review, baseline, decisions)
                self.assertNotIn("fixture-affiliation", json.dumps(result))
                self.assertEqual(operations[action], 3)
                reordered = copy.deepcopy(review)
                reordered["findings"].reverse()
                reordered["review_id"] = review_identity(reordered)
                decisions["review_id"] = reordered["review_id"]
                self.assertEqual(transform(reordered, baseline, decisions)[0], result)

    def test_legacy_semantic_refused_before_backend_and_v1_transform_unchanged(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = self.fixture(root, ["fixture-affiliation"], custom=["fixture-affiliation"])
            _, baseline = load_review(root / "review")
            findings = {item["id"]: item for item in original["findings"]}
            add_finding(findings, "fixture-affiliation", (0, "data", "content"), 0, 19, "inference", detector="copilot", reason="Legacy assessment")
            original["findings"] = list(findings.values())
            original["semantic"]["status"] = "reviewed"
            original["review_id"] = review_identity(original)
            write_json(root / "review/review.json", original)
            before = (root / "review/review.json").read_bytes()
            backend = Backend([])
            with self.assertRaisesRegex(ValueError, "fresh scan"):
                semantic_review(root / "review", consent=True, backend=backend)
            self.assertEqual(backend.calls, [])
            self.assertEqual((root / "review/review.json").read_bytes(), before)
            self.assertFalse((root / "review/audit").exists())
            loaded, _ = load_review(root / "review")
            decisions = self.choices(loaded, "pseudonymize")
            decisions["choices"][loaded["findings"][1]["id"]] = {"action": "generalize", "replacement": "legacy alternative"}
            result, _ = transform(loaded, baseline, decisions)
            self.assertEqual(result[0]["data"]["content"], "[ENTITY_1]")

    def test_later_window_failure_does_not_promote_or_modify_choices(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = self.fixture(root, ["fixture-affiliation " + "first " * 20, "Second invented field " + "second " * 20])
            before = (root / "review/review.json").read_bytes()
            write_json(root / "review/decisions.json", self.choices(original))
            choice_bytes = (root / "review/decisions.json").read_bytes()
            bad = self.proposal("S2", "uncertain", "Invalid quote")
            backend = Backend([{"findings": [self.proposal("S1", "necessary", "First window")]}, {"findings": [bad]}, {"findings": [bad]}])
            with self.assertRaisesRegex(ValueError, "no approval or partial"):
                semantic_review(root / "review", consent=True, backend=backend, chunk_chars=200)
            self.assertEqual(len(backend.calls), 3)
            self.assertEqual((root / "review/review.json").read_bytes(), before)
            self.assertEqual((root / "review/decisions.json").read_bytes(), choice_bytes)
            self.assertFalse((root / "review/audit").exists())

    def test_repeated_v2_review_preserves_prior_audit_and_requires_new_approval(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root, ["fixture-affiliation"])
            first = semantic_review(root / "review", consent=True, backend=Backend([{"findings": [self.proposal("S1", "necessary", "First assessment")]}]))
            first_bytes = (root / "review/review.json").read_bytes()
            second = semantic_review(root / "review", consent=True, backend=Backend([{"findings": [self.proposal("S1", "unnecessary", "Second assessment")]}]))
            self.assertEqual(len(second["findings"][0]["assessments"]), 2)
            self.assertEqual(second["findings"][0]["necessity"], "uncertain")
            self.assertNotEqual(first["review_id"], second["review_id"])
            self.assertEqual((root / "review/audit" / ("review-" + second["parent_review"]["sha256"] + ".json")).read_bytes(), first_bytes)
            _, baseline = load_review(root / "review")
            with self.assertRaisesRegex(ValueError, "match this review"):
                transform(second, baseline, self.choices(first))

    def test_v2_parent_and_scope_tampering_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root, ["fixture@example.invalid"])
            review = semantic_review(root / "review", consent=True, backend=Backend([{"findings": []}]))
            for change in ("scope", "policy", "unscoped"):
                changed = copy.deepcopy(review)
                if change == "scope":
                    changed["findings"][0]["scope"]["field_sha256"] = "0" * 64
                elif change == "policy":
                    changed["semantic"]["finding_scope"] = "unknown"
                else:
                    del changed["findings"][0]["scope"]
                changed["review_id"] = review_identity(changed)
                write_json(root / "review/review.json", changed)
                with self.assertRaises(ValueError):
                    load_review(root / "review")
            write_json(root / "review/review.json", review)
            parent = root / "review/audit" / ("review-" + review["parent_review"]["sha256"] + ".json")
            parent.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Parent privacy audit"):
                load_review(root / "review")

    def test_same_context_local_categories_and_semantic_share_one_decision(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = self.fixture(root, ["fixture@example.invalid", "fixture@example.invalid"])
            response = {"findings": [{"slot": "S1", "quote": "[ENTITY_1]", "category": "inference", "necessity": "unnecessary", "reason": "Invented context", "alternative": "", "related": []}]}
            review = semantic_review(root / "review", consent=True, backend=Backend([response]))
            self.assertEqual(len(review["findings"]), 1)
            finding = review["findings"][0]
            self.assertEqual(finding["categories"], ["identifier", "inference"])
            self.assertEqual(len(finding["occurrences"]), 2)
            self.assertEqual(finding["local_judgments"][0]["category"], "identifier")
            self.assertEqual(finding["assessments"][0]["category"], "inference")
            self.assertEqual(finding["assessments"][0]["sources"], [{"review_id": original["review_id"], "pass": "privacy-context-1", "slot": "S1", "path": [0, "data", "content"]}])

    def test_multiwindow_contradictions_retain_actual_pass_provenance(self):
        class WindowBackend:
            def __init__(self):
                self.calls = []

            def generate(self, prompt, label):
                self.calls.append({"label": label})
                supplied = json.loads(prompt.split("SOURCE SLOTS:\n", 1)[1])
                findings = []
                for item in supplied:
                    if "fixture-affiliation" in item["text"]:
                        findings.append({"slot": item["slot"], "quote": "fixture-affiliation", "category": "inference",
                                         "necessity": "necessary" if label == "privacy-cross-context" else "unnecessary",
                                         "reason": "Assessment in " + label, "alternative": "", "related": []})
                return {"findings": findings}

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root, ["fixture-affiliation " + "first " * 10, "Different field " + "second " * 20])
            backend = WindowBackend()
            review = semantic_review(root / "review", consent=True, backend=backend, chunk_chars=240)
            self.assertGreater(review["semantic"]["windows"], 1)
            finding = review["findings"][0]
            self.assertEqual(finding["necessity"], "uncertain")
            self.assertEqual(len(finding["assessments"]), 2)
            self.assertEqual({source["pass"] for assessment in finding["assessments"] for source in assessment["sources"]}, {"privacy-context-1", "privacy-cross-context"})

    def test_partial_overlap_is_refused_even_under_a_containing_removal(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root, ["alpha beta gamma"])
            proposals = [{"slot": "S1", "quote": quote, "category": "inference", "necessity": "unnecessary",
                          "reason": "Synthetic overlap", "alternative": "", "related": []}
                         for quote in ("alpha beta", "beta gamma", "alpha beta gamma")]
            review = semantic_review(root / "review", consent=True, backend=Backend([{"findings": proposals}]))
            _, baseline = load_review(root / "review")
            with self.assertRaisesRegex(ValueError, "Partially overlapping"):
                transform(review, baseline, self.choices(review, "remove"))

    def test_containment_keeps_existing_larger_span_behavior_and_keep_guard(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root, ["Private fixture@example.invalid"])
            response = {"findings": [{"slot": "S1", "quote": "Private [ENTITY_1]", "category": "inference", "necessity": "unnecessary",
                                      "reason": "Synthetic containing span", "alternative": "", "related": []}]}
            review = semantic_review(root / "review", consent=True, backend=Backend([response]))
            _, baseline = load_review(root / "review")
            decisions = self.choices(review, "remove")
            result, operations = transform(review, baseline, decisions)
            self.assertEqual(result[0]["data"]["content"].count("PRIVATE_DETAIL"), 1)
            self.assertEqual(operations["remove"], 2)
            smaller = next(item for item in review["findings"] if item["text"] == "fixture@example.invalid")
            decisions["choices"][smaller["id"]] = {"action": "keep"}
            with self.assertRaisesRegex(ValueError, "overlaps"):
                transform(review, baseline, decisions)

    def test_field_scope_uses_sanitized_text_not_raw_credentials_or_normalization(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            quote = "fixture-affiliation"
            self.fixture(root, [quote + " api_key=InventedAlphaCredential1234", quote + " api_key=InventedBravoCredential5678", quote + " "])
            review = semantic_review(root / "review", consent=True, backend=Backend([{"findings": [self.proposal("S1", "unnecessary", "Synthetic sanitized context")]}]))
            self.assertEqual(len(review["findings"]), 1)
            self.assertEqual(len(review["findings"][0]["occurrences"]), 2)
            _, baseline = load_review(root / "review")
            result, _ = transform(review, baseline, self.choices(review, "remove"))
            self.assertEqual(result[2]["data"]["content"], quote + " ")
            self.assertNotIn("Credential", json.dumps(result))
            self.assertGreater(review["hard_removals"]["secret_pattern_matches"], 0)

    def test_v2_replay_masks_identifier_even_with_another_display_category(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root, ["fixture@example.invalid"])
            response = {"findings": [{"slot": "S1", "quote": "[ENTITY_1]", "category": "health", "necessity": "uncertain",
                                      "reason": "Invented detector classification, not a verified attribute", "alternative": "", "related": []}]}
            first = semantic_review(root / "review", consent=True, backend=Backend([response]))
            self.assertEqual(first["findings"][0]["category"], "health")
            backend = Backend([{"findings": []}])
            semantic_review(root / "review", consent=True, backend=backend)
            self.assertNotIn("fixture@example.invalid", backend.prompts[0])
            self.assertIn("[ENTITY_1]", backend.prompts[0])

    def test_audit_ancestry_is_verified_after_multiple_reviews(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root, ["fixture@example.invalid"])
            first = semantic_review(root / "review", consent=True, backend=Backend([{"findings": []}]))
            semantic_review(root / "review", consent=True, backend=Backend([{"findings": []}]))
            original_audit = root / "review/audit" / ("review-" + first["parent_review"]["sha256"] + ".json")
            original_audit.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Parent privacy audit"):
                load_review(root / "review")

    def test_source_change_during_mock_review_prevents_promotion(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root, ["fixture-affiliation"])
            before = (root / "review/review.json").read_bytes()

            class ChangingBackend:
                calls = []

                def generate(self, prompt, label):
                    source = root / "source/events.jsonl"
                    source.write_bytes(source.read_bytes() + b"\n")
                    return {"findings": []}

            with self.assertRaisesRegex(ValueError, "changed"):
                semantic_review(root / "review", consent=True, backend=ChangingBackend())
            self.assertEqual((root / "review/review.json").read_bytes(), before)
            self.assertFalse((root / "review/audit").exists())

    def test_local_combination_groups_partition_without_losing_their_judgments(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            quote = "I work at Imaginary Workshop."
            original = self.fixture(root, [quote, quote + " Keep the failed test.", "I live in Invented Borough."])
            prior = next(item for item in original["findings"] if item["text"] == quote)
            self.assertEqual(len(prior["occurrences"]), 2)
            review = semantic_review(root / "review", consent=True, backend=Backend([{"findings": []}]))
            partitions = [item for item in review["findings"] if item["text"] == quote]
            self.assertEqual(len(partitions), 2)
            decisions = self.choices(review)
            for finding in partitions:
                local = finding["local_judgments"][0]
                self.assertEqual(local["detectors"], ["local-combination"])
                self.assertEqual(local["related"], prior["related"])
                self.assertEqual(local["reason"], prior["reason"])
                self.assertEqual(local["source_finding_id"], prior["id"])
                if finding["occurrences"][0]["path"][0] == 1:
                    decisions["choices"][finding["id"]] = {"action": "remove"}
            _, baseline = load_review(root / "review")
            result, _ = transform(review, baseline, decisions)
            self.assertEqual(result[0]["data"]["content"], quote)
            self.assertNotIn(quote, result[1]["data"]["content"])
            self.assertIn("Keep the failed test.", result[1]["data"]["content"])

    def test_related_field_does_not_become_a_target_and_duplicate_assessment_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root, ["fixture-affiliation", "Fixture context fixture-affiliation"])
            proposal = self.proposal("S1", "unnecessary", "Synthetic selected field")
            proposal["related"] = ["S2"]
            review = semantic_review(root / "review", consent=True, backend=Backend([{"findings": [proposal, proposal]}]))
            self.assertEqual(len(review["findings"]), 1)
            finding = review["findings"][0]
            self.assertEqual(len(finding["assessments"]), 1)
            self.assertEqual(len(finding["occurrences"]), 1)
            self.assertEqual(finding["related"], ["S2"])
            _, baseline = load_review(root / "review")
            result, _ = transform(review, baseline, self.choices(review, "remove"))
            self.assertEqual(result[1]["data"]["content"], "Fixture context fixture-affiliation")


class PrivacyPublicationTests(unittest.TestCase):
    setUp = PrivacyContextScopeTests.setUp
    fixture = PrivacyContextScopeTests.fixture
    choices = PrivacyContextScopeTests.choices
    proposal = PrivacyContextScopeTests.proposal

    def test_conflicting_final_snapshot_is_refused_before_backend(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root, ["fixture-affiliation"])
            before = (root / "review/review.json").read_bytes()
            audit = root / "review/audit"
            audit.mkdir()
            parent = audit / ("review-" + hashlib.sha256(before).hexdigest() + ".json")
            parent.write_bytes(b"incomplete synthetic audit")
            backend = Backend([])
            with self.assertRaisesRegex(ValueError, "Existing parent"):
                semantic_review(root / "review", consent=True, backend=backend)
            self.assertEqual(backend.calls, [])
            self.assertEqual(parent.read_bytes(), b"incomplete synthetic audit")
            self.assertEqual((root / "review/review.json").read_bytes(), before)

    def test_partial_snapshot_write_cleans_stage_and_retry_succeeds(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = self.fixture(root, ["fixture-affiliation"])
            write_json(root / "review/decisions.json", self.choices(original))
            before = {name: (root / "review" / name).read_bytes() for name in ("review.json", "decisions.json")}
            actual_fdopen = semantic_helpers.os.fdopen

            @contextmanager
            def interrupted_fdopen(descriptor, mode):
                with actual_fdopen(descriptor, mode) as stream:
                    if mode == "wb":
                        class InterruptedStream:
                            def write(self, content):
                                stream.write(content[:31])
                                stream.flush()
                                raise OSError("synthetic partial snapshot write")
                        yield InterruptedStream()
                    else:
                        yield stream

            with patch.object(semantic_helpers.os, "fdopen", interrupted_fdopen):
                with self.assertRaisesRegex(OSError, "partial snapshot"):
                    semantic_review(root / "review", consent=True, backend=Backend([{"findings": []}]))
            self.assertEqual(list((root / "review/audit").iterdir()), [])
            self.assertEqual(before, {name: (root / "review" / name).read_bytes() for name in before})
            semantic_review(root / "review", consent=True, backend=Backend([{"findings": []}]))
            load_review(root / "review")

    def test_unverified_stage_is_never_published(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root, ["fixture-affiliation"])
            before = (root / "review/review.json").read_bytes()
            actual_read = Path.read_bytes

            def changed_stage(path):
                return b"synthetic corruption" if path.name.startswith(".privacy-stage-") else actual_read(path)

            with patch.object(Path, "read_bytes", changed_stage):
                with self.assertRaisesRegex(ValueError, "staged bytes"):
                    semantic_review(root / "review", consent=True, backend=Backend([{"findings": []}]))
            self.assertEqual(list((root / "review/audit").iterdir()), [])
            self.assertEqual((root / "review/review.json").read_bytes(), before)

    def test_snapshot_link_race_never_overwrites_existing_audit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root, ["fixture-affiliation"])
            before = (root / "review/review.json").read_bytes()
            actual_link = semantic_helpers.os.link
            published = []

            def competing_link(source, destination):
                destination.write_bytes(b"synthetic competing audit")
                published.append(destination)
                return actual_link(source, destination)

            with patch.object(semantic_helpers.os, "link", competing_link):
                with self.assertRaisesRegex(ValueError, "Existing parent"):
                    semantic_review(root / "review", consent=True, backend=Backend([{"findings": []}]))
            self.assertEqual(published[0].read_bytes(), b"synthetic competing audit")
            self.assertEqual((root / "review/review.json").read_bytes(), before)
            self.assertEqual(list((root / "review/audit").glob(".privacy-stage-*")), [])

    def test_busy_publication_lock_rejects_before_backend_and_releases(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root, ["fixture-affiliation"])
            backend = Backend([])
            with semantic_helpers.publication_lock(root / "review"):
                with self.assertRaisesRegex(ValueError, "busy"):
                    semantic_review(root / "review", consent=True, backend=backend)
            self.assertEqual(backend.calls, [])
            semantic_review(root / "review", consent=True, backend=Backend([{"findings": []}]))

    def test_matching_immutable_snapshot_is_reused_without_writing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root, ["fixture-affiliation"])
            before = (root / "review/review.json").read_bytes()
            audit = root / "review/audit"
            audit.mkdir()
            parent = audit / ("review-" + hashlib.sha256(before).hexdigest() + ".json")
            parent.write_bytes(before)
            modified = parent.stat().st_mtime_ns
            with patch.object(semantic_helpers.os, "link", side_effect=AssertionError("Existing audits cannot be republished")):
                semantic_review(root / "review", consent=True, backend=Backend([{"findings": []}]))
            self.assertEqual(parent.read_bytes(), before)
            self.assertEqual(parent.stat().st_mtime_ns, modified)

    def test_unsupported_snapshot_link_fails_closed_and_releases_lock(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root, ["fixture-affiliation"])
            before = (root / "review/review.json").read_bytes()
            with patch.object(semantic_helpers.os, "link", side_effect=OSError("synthetic unsupported hard links")):
                with self.assertRaisesRegex(OSError, "unsupported hard links"):
                    semantic_review(root / "review", consent=True, backend=Backend([{"findings": []}]))
            self.assertEqual((root / "review/review.json").read_bytes(), before)
            self.assertEqual(list((root / "review/audit").iterdir()), [])
            semantic_review(root / "review", consent=True, backend=Backend([{"findings": []}]))

    def test_pass_that_finishes_after_another_publisher_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root, ["fixture-affiliation"])
            inner = {}
            inner_backend = Backend([{"findings": [self.proposal("S1", "necessary", "Completed inner review")]}])

            class InterleavedBackend:
                calls = []

                def generate(self, prompt, label):
                    self.calls.append({"label": label})
                    inner["review"] = semantic_review(root / "review", consent=True, backend=inner_backend)
                    inner["bytes"] = (root / "review/review.json").read_bytes()
                    return {"findings": []}

            with self.assertRaisesRegex(ValueError, "changed during"):
                semantic_review(root / "review", consent=True, backend=InterleavedBackend())
            self.assertEqual((root / "review/review.json").read_bytes(), inner["bytes"])
            self.assertEqual(load_review(root / "review")[0], inner["review"])

    def test_lock_is_held_until_review_publication_finishes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root, ["fixture-affiliation"])
            actual_write = semantic_helpers.write_json
            nested = Backend([])
            checked = []

            def guarded_write(path, value):
                if Path(path).resolve() == (root / "review/review.json").resolve():
                    with self.assertRaisesRegex(ValueError, "busy"):
                        semantic_review(root / "review", consent=True, backend=nested)
                    checked.append(True)
                return actual_write(path, value)

            with patch.object(semantic_helpers, "write_json", guarded_write):
                semantic_review(root / "review/../review", consent=True, backend=Backend([{"findings": []}]))
            self.assertEqual(checked, [True])
            self.assertEqual(nested.calls, [])

    def test_replace_failure_keeps_review_and_uses_new_temps_on_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root, ["fixture-affiliation"])
            before = (root / "review/review.json").read_bytes()
            actual_replace = Path.replace
            stages = []

            def fail_first_replace(path, destination):
                stages.append(path)
                if len(stages) == 1:
                    raise OSError("synthetic replace failure")
                return actual_replace(path, destination)

            with patch.object(Path, "replace", fail_first_replace):
                with self.assertRaisesRegex(OSError, "replace failure"):
                    semantic_review(root / "review", consent=True, backend=Backend([{"findings": []}]))
                self.assertEqual((root / "review/review.json").read_bytes(), before)
                semantic_review(root / "review", consent=True, backend=Backend([{"findings": []}]))
            self.assertEqual(len(set(stages)), 2)
            self.assertTrue(all(not path.exists() for path in stages))
            load_review(root / "review")

    def test_load_checks_structural_semantic_provenance(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root, ["fixture-affiliation", "Unrelated synthetic field"])
            review = semantic_review(root / "review", consent=True, backend=Backend([
                {"findings": [self.proposal("S1", "uncertain", "Synthetic assessment")]}]))
            for change in ("empty", "review", "slot", "path", "pass"):
                with self.subTest(change=change):
                    changed = copy.deepcopy(review)
                    sources = changed["findings"][0]["assessments"][0]["sources"]
                    if change == "empty":
                        sources.clear()
                    elif change == "review":
                        sources[0]["review_id"] = "not-an-ancestor"
                    elif change == "slot":
                        sources[0]["slot"] = "S2"
                    elif change == "path":
                        sources[0]["path"] = [1, "data", "content"]
                    else:
                        sources[0]["pass"] = "unrecorded-pass"
                    changed["review_id"] = review_identity(changed)
                    write_json(root / "review/review.json", changed)
                    with self.assertRaises(ValueError):
                        load_review(root / "review")

    def test_load_checks_local_judgment_against_bound_parent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root, ["Contact fixture@example.invalid"])
            review = semantic_review(root / "review", consent=True, backend=Backend([{"findings": []}]))
            for change in ("source_finding_id", "reason"):
                with self.subTest(change=change):
                    changed = copy.deepcopy(review)
                    finding = changed["findings"][0]
                    finding["local_judgments"][0][change] = "not in original parent"
                    if change == "reason":
                        finding["reason"] = "Local: not in original parent"
                    changed["review_id"] = review_identity(changed)
                    write_json(root / "review/review.json", changed)
                    with self.assertRaisesRegex(ValueError, "Local judgment provenance"):
                        load_review(root / "review")


if __name__ == "__main__":
    unittest.main()
