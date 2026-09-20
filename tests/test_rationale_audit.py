import copy
import json
import tempfile
import unittest
from pathlib import Path

from session_spec.rationale_audit import MAX_SOURCE_CHARS, SCHEMA, rationale_focus, validate_rationale_audit
from session_spec.story_article import validate_article
from session_spec.story_editor import generate_edition
from test_story_pipeline import FakeBackend, article, brief, edition_review, insights, packet


PATH = "/article/agent_detail/trajectory/0/rationale"


def fixture(basis="recorded"):
    events = packet()
    events[0]["text"] = events[0]["human_input"] = "Keep the wrapper because it preserves the existing startup path."
    draft = {"article": article(), "brief": brief(), "insights": insights()}
    draft["article"]["agent_detail"]["trajectory"][0]["rationale"] = {
        "basis": basis, "text": "The user chose to retain the wrapper to preserve the existing startup path.", "refs": ["E000001"]}
    return draft, events


def audit(assessment="expressed_reason", status="supported"):
    review = edition_review()
    review["rationale_audit"] = [{"path": PATH, "assessment": assessment, "status": status,
                                  "note": "The human explicitly supplied a preservation reason.",
                                  "evidence": [{"ref": "E000001", "origin": "human", "quote": "because it preserves the existing startup path"}]}]
    return review


class RationaleAuditTests(unittest.TestCase):
    def test_explicit_reason_is_source_local_without_mutation(self):
        draft, events = fixture()
        before = copy.deepcopy((draft, events))
        focus = rationale_focus(draft, events)
        self.assertEqual(validate_rationale_audit(audit(), focus, events), [])
        self.assertEqual(focus["sources"][0]["ref"], "E000001")
        self.assertEqual((draft, events), before)

    def test_action_result_and_inference_cannot_pass_as_recorded(self):
        draft, events = fixture()
        for assessment in ("action_or_result_only", "inference", "retrospective", "unsupported"):
            with self.subTest(assessment=assessment):
                errors = validate_rationale_audit(audit(assessment), rationale_focus(draft, events), events)
                self.assertTrue(any("requires expressed_reason" in error for error in errors))

    def test_honest_inference_is_accepted_but_does_not_become_recorded(self):
        draft, events = fixture("inferred")
        self.assertEqual(validate_rationale_audit(audit("inference"), rationale_focus(draft, events), events), [])
        self.assertTrue(validate_rationale_audit(audit(), rationale_focus(draft, events), events))

    def test_wrong_role_nonliteral_and_hidden_quotes_fail(self):
        draft, events = fixture()
        events[0]["reasoningText"] = "HIDDEN_REASON_CANARY"
        focus = rationale_focus(draft, events)
        self.assertNotIn("HIDDEN_REASON_CANARY", json.dumps(focus))
        for span in ({"ref": "E000001", "origin": "assistant", "quote": "because it preserves the existing startup path"},
                     {"ref": "E000001", "origin": "human", "quote": "because it is fastest"},
                     {"ref": "E000001", "origin": "human", "quote": "HIDDEN_REASON_CANARY"},
                     {"ref": ["E000001"], "origin": "human", "quote": "because"}):
            with self.subTest(span=span):
                review = audit()
                review["rationale_audit"][0]["evidence"] = [span]
                self.assertTrue(validate_rationale_audit(review, focus, events))

    def test_undeclared_support_cannot_silently_fix_a_claim(self):
        draft, events = fixture()
        draft["article"]["agent_detail"]["trajectory"][0]["rationale"]["refs"] = ["E000002"]
        errors = validate_rationale_audit(audit(), rationale_focus(draft, events), events)
        self.assertTrue(any("declared refs" in error for error in errors))

    def test_later_explanation_does_not_backdate_an_earlier_understanding(self):
        draft, events = fixture()
        events.append({"ref": "E000003", "type": "assistant.message", "text": "I later realized this preserves the startup path."})
        phases = draft["article"]["agent_detail"]["trajectory"]
        phases[0]["rationale"]["refs"].append("E000003")
        phases.append({"id": "later", "refs": ["E000003"], "rationale": {"basis": "not_recorded"}})
        review = audit()
        review["rationale_audit"][0]["evidence"] = [{"ref": "E000003", "origin": "assistant", "quote": "I later realized this preserves the startup path."}]
        errors = validate_rationale_audit(review, rationale_focus(draft, events), events)
        self.assertTrue(any("later-phase source" in error for error in errors))
        review["rationale_audit"][0]["timing"] = {"assessment": "retrospective", "note": "The source says the actor only realized this later."}
        self.assertTrue(validate_rationale_audit(review, rationale_focus(draft, events), events))
        phases[0]["rationale"]["basis"] = "inferred"
        review["rationale_audit"][0]["assessment"] = "inference"
        self.assertEqual(validate_rationale_audit(review, rationale_focus(draft, events), events), [])

    def test_needs_fix_must_point_to_this_rationale_not_an_unrelated_finding(self):
        draft, events = fixture()
        review = audit("action_or_result_only", "needs_fix")
        review["rationale_audit"][0]["evidence"] = []
        review["issues"] = [{"path": "/article/title"}]
        self.assertTrue(validate_rationale_audit(review, rationale_focus(draft, events), events))
        review["issues"] = [{"path": PATH + "/text"}]
        self.assertEqual(validate_rationale_audit(review, rationale_focus(draft, events), events), [])

    def test_shared_phase_anchors_do_not_create_a_false_temporal_boundary(self):
        draft, events = fixture()
        phases = draft["article"]["agent_detail"]["trajectory"]
        phases.append({"id": "overlapping", "refs": ["E000001", "E000002"], "rationale": {"basis": "not_recorded"}})
        focus = rationale_focus(draft, events)
        self.assertTrue(focus["rationales"][0]["overlapping_phase_anchors"])
        self.assertIsNone(focus["rationales"][0]["next_phase_start"])
        self.assertEqual(validate_rationale_audit(audit(), focus, events), [])

    def test_shared_transition_reason_needs_no_duplicate_aggregate_citation(self):
        draft, events = fixture()
        events.append({"ref": "E000003", "type": "assistant.message",
                       "text": "I will keep the wrapper because it preserves the existing startup path. Next I will validate the handoff."})
        phases = draft["article"]["agent_detail"]["trajectory"]
        phases[0]["rationale"] = {"basis": "recorded", "text": "At the transition the assistant chose the wrapper to preserve startup.", "refs": ["E000003"]}
        following = copy.deepcopy(phases[0])
        following.update(id="next-phase", refs=["E000003"], human_refs=[], tool_refs=[], tool_steps=[],
                         rationale={"basis": "not_recorded", "text": "", "refs": []})
        phases.append(following)
        review = audit()
        row = review["rationale_audit"][0]
        row["evidence"] = [{"ref": "E000003", "origin": "assistant", "quote": "I will keep the wrapper because it preserves the existing startup path."}]
        row["timing"] = {"assessment": "shared_transition", "note": "The same message expresses the wrapping reason and then starts validation; the claim attributes it to that transition."}
        for duplicate in (False, True):
            with self.subTest(duplicate=duplicate), tempfile.TemporaryDirectory() as temporary:
                candidate = copy.deepcopy(draft)
                if duplicate:
                    candidate["article"]["agent_detail"]["trajectory"][0]["refs"].append("E000003")
                self.assertEqual(validate_article(candidate["article"], events), [])
                self.assertEqual(validate_rationale_audit(review, rationale_focus(candidate, events), events), [])
                backend = FakeBackend([review])
                self.assertEqual(generate_edition(Path(temporary), candidate, events, backend, validate_article), candidate)
                self.assertEqual(len(backend.calls), 1)

    def test_temporal_adjudication_does_not_excuse_false_source_or_role(self):
        draft, events = fixture()
        phases = draft["article"]["agent_detail"]["trajectory"]
        events.append({"ref": "E000003", "type": "assistant.message", "text": "An observable transition."})
        phases[0]["rationale"]["refs"] = ["E000003"]
        phases.append({"id": "next", "refs": ["E000003"], "rationale": {"basis": "not_recorded"}})
        review = audit()
        review["rationale_audit"][0]["timing"] = {"assessment": "shared_transition", "note": "A claimed shared boundary is not proof of the quote."}
        review["rationale_audit"][0]["evidence"] = [{"ref": "E000003", "origin": "assistant", "quote": "An invented transition."}]
        self.assertTrue(validate_rationale_audit(review, rationale_focus(draft, events), events))

    def test_missing_duplicate_and_unknown_rows_fail_closed(self):
        draft, events = fixture()
        focus = rationale_focus(draft, events)
        for rows in ([], None, [None], [{"path": "/article/title"}], audit()["rationale_audit"] * 2):
            with self.subTest(rows=rows):
                self.assertTrue(validate_rationale_audit({"rationale_audit": rows}, focus, events))
        draft["article"]["agent_detail"]["trajectory"][0]["rationale"]["basis"] = "not_recorded"
        self.assertEqual(validate_rationale_audit({}, rationale_focus(draft, events), events), [])

    def test_audit_is_candidate_bound_cached_and_does_not_add_a_model_call(self):
        draft, events = fixture()
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            backend = FakeBackend([audit()])
            generate_edition(directory, draft, events, backend, validate_article)
            self.assertEqual(len(backend.calls), 1)
            self.assertIn("RATIONALE_FOCUS", backend.prompts[0])
            receipt = json.loads((directory / "edition-receipt.json").read_bytes())
            focus = json.loads((directory / "edition-rationales-0.json").read_bytes())
            self.assertEqual(focus["candidate_sha256"], receipt["candidate_sha256"])
            self.assertEqual(receipt["identity"]["rationale_audit"], SCHEMA)
            cached = FakeBackend([])
            generate_edition(directory, draft, events, cached, validate_article)
            self.assertFalse(cached.calls)

    def test_missing_audit_gets_one_review_repair_instead_of_silent_acceptance(self):
        draft, events = fixture()
        with tempfile.TemporaryDirectory() as temporary:
            backend = FakeBackend([edition_review(), audit()])
            generate_edition(Path(temporary), draft, events, backend, validate_article)
            self.assertEqual(len(backend.calls), 2)
            self.assertIn("Rationale audit must cover", backend.prompts[1])

    def test_source_excerpts_are_deduplicated_bounded_and_omissions_explicit(self):
        draft, events = fixture()
        references = []
        for index in range(10, 50):
            reference = f"E{index:06d}"
            events.append({"ref": reference, "type": "assistant.message", "text": "observable " * 1000})
            references.append(reference)
        phase = draft["article"]["agent_detail"]["trajectory"][0]
        phase["rationale"]["refs"] = references * 2
        focus = rationale_focus(draft, events)
        self.assertEqual(focus["rationales"][0]["refs"], references)
        self.assertEqual(len(focus["sources"]) + len(focus["omitted_source_refs"]), len(references))
        self.assertTrue(all(source["truncated"] for source in focus["sources"]))
        self.assertLessEqual(sum(len(segment["text"]) for source in focus["sources"] for segment in source["segments"]), MAX_SOURCE_CHARS)


if __name__ == "__main__":
    unittest.main()
