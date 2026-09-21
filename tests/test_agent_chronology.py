import copy
import unittest

from offline_provider import guard_offline_test
from session_spec.agent_handoff import CHRONOLOGY_POLICY, validate_agent_detail
from session_spec.storage import PROMPTS
from test_story_pipeline import article


class AgentChronologyTests(unittest.TestCase):
    def setUp(self):
        guard_offline_test(self)
        human_numbers = {3, 24, 28, 32, 36, 40}
        self.events = []
        for number in range(1, 42):
            event = {"ref": f"E{number:06}", "origin": "root", "type": "assistant.message", "text": f"Synthetic observed statement {number}"}
            if number in human_numbers:
                event.update(type="user.message", human_input=f"Synthetic request {number}")
            self.events.append(event)
        self.detail = article()["agent_detail"]
        self.prototype = copy.deepcopy(self.detail["trajectory"][0])
        self.set_phases((
            ("phase-one", [3], [22, 23]),
            ("phase-two", [24, 28, 36], [37, 39]),
            ("phase-three", [32, 40], [34, 39, 41]),
        ))

    def set_phases(self, phases):
        self.detail["trajectory"] = []
        for identifier, human_refs, refs in phases:
            phase = copy.deepcopy(self.prototype)
            phase.update(id=identifier, human_refs=[f"E{number:06}" for number in human_refs],
                         refs=[f"E{number:06}" for number in refs], tool_refs=[], tool_steps=[])
            self.detail["trajectory"].append(phase)
        self.detail["paths"][0]["phase_ids"] = [phase["id"] for phase in self.detail["trajectory"]]

    def chronology(self):
        return [issue for issue in validate_agent_detail(self.detail, self.events) if "source chronology" in issue]

    def test_run02_style_topics_use_human_involvement_without_inserting_refs(self):
        original = copy.deepcopy(self.detail)
        events = copy.deepcopy(self.events)
        self.assertEqual(validate_agent_detail(self.detail, self.events), [])
        self.assertEqual(self.detail, original)
        self.assertEqual(self.events, events)

    def test_run02_repair_style_duplicate_initiator_is_not_required_or_removed(self):
        self.detail["trajectory"][2]["refs"] = ["E000032", "E000039", "E000041"]
        original = copy.deepcopy(self.detail)
        self.assertEqual(validate_agent_detail(self.detail, self.events), [])
        self.assertEqual(self.detail, original)

    def test_run05_style_installer_topic_can_close_after_concurrency_topic(self):
        self.set_phases((
            ("matrix-phase", [3], [23]),
            ("installer-phase", [24, 28], [37, 39]),
            ("concurrency-phase", [32], [34, 35]),
            ("npm-path-phase", [36, 40], [39, 41]),
        ))
        original = copy.deepcopy(self.detail)
        self.assertEqual(validate_agent_detail(self.detail, self.events), [])
        self.assertEqual(self.detail, original)

    def test_actual_reversed_minima_report_both_phase_owned_sets_without_mutation(self):
        phases = self.detail["trajectory"]
        phases[1], phases[2] = phases[2], phases[1]
        original = copy.deepcopy(self.detail)
        issues = self.chronology()
        self.assertEqual(len(issues), 1)
        for text in (CHRONOLOGY_POLICY, "/article/agent_detail/trajectory/2", "/article/agent_detail/trajectory/1",
                     '"refs": ["E000037", "E000039"]', '"human_refs": ["E000024", "E000028", "E000036"]',
                     '"human_refs": ["E000032", "E000040"]', '"tool_refs": []',
                     "earliest=E000024 at source position=23", "earliest=E000032 at source position=31",
                     "validated union", "not actual initiation", "Inspect BOTH phases",
                     "Retain legitimate facts, refs and user coverage"):
            self.assertIn(text, issues[0])
        self.assertEqual(self.detail, original)

    def test_rationale_and_global_human_refs_cannot_backdate_reversed_phases(self):
        phases = self.detail["trajectory"]
        phases[1], phases[2] = phases[2], phases[1]
        phases[2]["rationale"] = {"basis": "recorded", "text": "Synthetic background", "refs": ["E000001", "E000003"]}
        self.assertEqual(len(self.chronology()), 1)
        self.assertIn("earliest=E000024", self.chronology()[0])

    def test_invalid_intermediate_refs_do_not_misidentify_previous_valid_phase(self):
        self.detail["trajectory"][1]["refs"] = ["E999999"]
        self.detail["trajectory"][1]["human_refs"] = ["E999999"]
        self.detail["trajectory"][2]["refs"] = ["E000001", "E000039", "E000041"]
        issue = self.chronology()[0]
        self.assertIn("Previous /article/agent_detail/trajectory/0", issue)
        self.assertIn("earliest=E000003 at source position=2", issue)
        self.assertTrue(any("Invalid Agent references" in issue for issue in validate_agent_detail(self.detail, self.events)))

    def test_source_order_not_reference_number_and_equal_minima_remain_valid(self):
        self.events.insert(0, {"ref": "E900000", "origin": "root", "type": "assistant.message", "text": "Synthetic first event"})
        self.detail["trajectory"][0]["refs"] = ["E900000"]
        self.detail["trajectory"][2]["refs"].append("E000024")
        original = copy.deepcopy(self.detail)
        self.assertEqual(validate_agent_detail(self.detail, self.events), [])
        self.assertEqual(self.detail, original)

    def test_out_of_numeric_order_source_still_rejects_reversed_involvement(self):
        initiating = self.events.pop(23)
        self.events.insert(0, initiating)
        issue = self.chronology()[0]
        self.assertIn("earliest=E000024 at source position=0", issue)
        self.assertIn("Previous /article/agent_detail/trajectory/0", issue)

    def tool_started_phases(self):
        self.events = [
            {"ref": "E000001", "origin": "root", "type": "tool.execution_start"},
            {"ref": "E000002", "origin": "root", "type": "user.message", "human_input": "Synthetic later request"},
            {"ref": "E000003", "origin": "root", "type": "assistant.message"},
        ]
        self.detail["schema"] = "agent-detail/v1"
        self.set_phases((("tool-started", [], [3]), ("human-started", [2], [2])))
        self.detail["trajectory"][0]["tool_refs"] = ["E000001"]

    def test_valid_tool_reference_participates_before_later_support(self):
        self.tool_started_phases()
        original = copy.deepcopy(self.detail)
        self.assertEqual(validate_agent_detail(self.detail, self.events), [])
        self.assertEqual(self.detail, original)

    def test_invalid_roles_or_unknown_refs_never_supply_an_earlier_anchor(self):
        for field, value, expected in (
            ("tool_refs", ["E000002"], "must be real tool events"),
            ("human_refs", ["E000001"], "phase human_refs"),
            ("refs", ["E999999"], "Invalid Agent references: phase"),
        ):
            with self.subTest(field=field):
                self.tool_started_phases()
                self.detail["trajectory"][0]["tool_refs"] = []
                self.detail["trajectory"][0][field] = value
                original = copy.deepcopy(self.detail)
                issues = validate_agent_detail(self.detail, self.events)
                self.assertTrue(any(expected in issue for issue in issues))
                if field != "refs":
                    self.assertEqual(len(self.chronology()), 1)
                self.assertEqual(self.detail, original)

    def test_prompt_states_policy_and_semantic_limit_without_ref_insertion(self):
        prompt = (PROMPTS / "fast-story.md").read_text(encoding="utf-8")
        self.assertEqual(CHRONOLOGY_POLICY, "first-cited-phase-involvement/v1")
        for text in (CHRONOLOGY_POLICY, "validated union of each phase's refs, human_refs and tool_refs",
                     "successive phase minima must not decrease", "not actual initiation",
                     "No duplicate insertion into refs is needed", "Overlapping topics and equal minima are allowed",
                     "Source-grounded review must still check", "inspect BOTH phases",
                     "never invent earlier anchors or delete legitimate refs", "does not insert refs, mutate phases or sort output"):
            self.assertIn(text, prompt)
        self.assertNotIn("phase.refs ONLY", prompt)

    def test_explicit_entry_keeps_earlier_support_in_a_later_review_phase(self):
        phases = self.detail["trajectory"]
        phases[2]["refs"].extend(["E000016", "E000023"])
        self.assertEqual(len(self.chronology()), 1)
        for phase, entry in zip(phases, ("E000003", "E000024", "E000032")):
            phase["entry_ref"] = entry
        original = copy.deepcopy(self.detail)
        self.assertEqual(validate_agent_detail(self.detail, self.events), [])
        self.assertEqual(self.detail, original)
        phases[1], phases[2] = phases[2], phases[1]
        self.assertEqual(len(self.chronology()), 1)
        self.assertIn("explicit-phase-entry/v1", self.chronology()[0])

    def test_explicit_entry_requires_phase_owned_valid_evidence(self):
        for entry in (None, 3, "E999999", "E000040"):
            with self.subTest(entry=entry):
                self.detail["trajectory"][0]["entry_ref"] = entry
                self.assertTrue(any("entry_ref must name a real event" in issue for issue in validate_agent_detail(self.detail, self.events)))


if __name__ == "__main__":
    unittest.main()
