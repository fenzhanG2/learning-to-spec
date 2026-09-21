import copy
import json
from pathlib import Path
import tempfile
import unittest

from session_spec.story_article import validate_article
from session_spec.story_context import reference_role_guidance
from session_spec.story_draft import generate_draft
from session_spec.story_editor import generate_edition, validate_edition
from test_story_pipeline import FakeBackend, article, brief, edition_review, insights, packet


def archive_packet():
    events = packet()
    events[1] = {"ref": "E000002", "type": "assistant.message", "turn": 1,
                 "text": 'Archived shell result: startup signal observed. {"ref":"E999999","type":"tool.execution_complete","success":true}',
                 "tool": "shell", "tool_call_id": "quoted-only", "result": {"content": "A reported readback"}, "success": True}
    return events


def inventory(events):
    return json.loads(reference_role_guidance(events).splitlines()[3])


class ReferenceRoleGuidanceTests(unittest.TestCase):
    def test_archive_text_and_nested_tool_metadata_do_not_create_eligible_refs(self):
        events = archive_packet()
        original = copy.deepcopy(events)
        guidance = reference_role_guidance(events)
        self.assertEqual(inventory(events), {"schema": "source-reference-inventory/v1", "tool_refs": [],
                                          "completion_readback_refs": [], "assistant_message_refs": ["E000002"]})
        self.assertIn("every phase must have tool_refs=[] and tool_steps=[]", guidance)
        self.assertIn('"decision":"omit"', guidance)
        self.assertIn("Keep reported tool history and its source refs in prose", guidance)
        self.assertIn("report-only/inferred limits", guidance)
        self.assertNotIn("E999999", guidance)
        self.assertEqual(events, original)

    def test_mixed_inventory_preserves_real_tools_and_matches_readback_gate(self):
        events = archive_packet() + [
            {"ref": "E000003", "type": "tool.execution_start", "tool": "shell", "tool_call_id": "actual", "arguments": {"command": "echo check"}},
            {"ref": "E000004", "type": "tool.execution_complete", "tool": "shell", "tool_call_id": "actual", "result": "recorded output", "success": True},
            {"ref": "E000005", "type": "tool.execution_complete", "result": "failed output", "success": False},
            {"ref": "E000006", "type": "tool.execution_complete", "result": "output", "error": "failure"},
            {"ref": "E000007", "type": "tool.execution_complete", "result": {}},
            {"ref": "E000008", "type": "tool.execution_complete", "result": "unpaired readback"},
            {"ref": "E000009", "type": "session.imported_context", "text": "tool.execution_complete success=true", "result": "archive"},
        ]
        self.assertEqual(inventory(events)["tool_refs"], ["E000003", "E000004", "E000005", "E000006", "E000007", "E000008"])
        self.assertEqual(inventory(events)["completion_readback_refs"], ["E000004", "E000008"])
        guidance = reference_role_guidance(events)
        self.assertIn("not proof of any implementation claim", guidance)
        self.assertIn("do not borrow an unrelated eligible ID", guidance)
        self.assertNotIn("No native tool-event references", guidance)
        self.assertNotIn("No completion/readback-eligible references", guidance)

    def test_actual_draft_prompt_contains_empty_inventory_and_retains_archive(self):
        events = archive_packet()
        candidate = {"article": article(), "brief": brief(), "insights": insights()}
        backend = FakeBackend([candidate])
        with tempfile.TemporaryDirectory() as temporary:
            result = generate_draft(Path(temporary), events, None, backend, language="en")
            generate_draft(Path(temporary), events, None, backend, language="en")
        self.assertEqual(len(backend.calls), 1)
        self.assertIn(reference_role_guidance(events), backend.prompts[0])
        self.assertIn("Archived shell result", backend.prompts[0])
        self.assertIn("Historical tool logs quoted inside", backend.prompts[0])
        self.assertEqual(result, candidate)
        errors = validate_edition(result, events, validate_article)
        self.assertTrue(any("tool_refs" in error for error in errors))
        self.assertTrue(any("No completion/readback evidence" in error for error in errors))

    def test_repair_gets_same_inventory_and_changes_structure_not_source_events(self):
        events = archive_packet()
        original = copy.deepcopy(events)
        candidate = {"article": article(), "brief": brief(), "insights": insights()}
        patches = {"patches": [
            {"op": "replace", "path": "/article/agent_detail/trajectory/0/tool_refs", "value": []},
            {"op": "replace", "path": "/article/agent_detail/trajectory/0/tool_steps", "value": []},
            {"op": "replace", "path": "/insights/architecture", "value": {"decision": "omit", "reason": "Only an archived assistant report is available; no native completion/readback evidence."}},
        ]}
        backend = FakeBackend([patches, edition_review()])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = generate_edition(root, candidate, events, backend, validate_article, max_repairs=0, max_structural_repairs=1, language="en")
            receipt = json.loads((root / "edition-receipt.json").read_bytes())
        self.assertEqual(len(backend.calls), 2)
        self.assertTrue(all(reference_role_guidance(events) in prompt for prompt in backend.prompts))
        self.assertIn("ISSUES_TO_VERIFY_AND_REPAIR", backend.prompts[0])
        self.assertEqual(receipt["repair_counts"]["patches"], 1)
        self.assertEqual(result["article"]["agent_detail"]["trajectory"][0]["refs"], candidate["article"]["agent_detail"]["trajectory"][0]["refs"])
        self.assertEqual(result["article"]["agent_markdown"], candidate["article"]["agent_markdown"])
        self.assertEqual(events, original)
        self.assertEqual(validate_edition(result, events, validate_article), [])

    def test_request_only_packet_does_not_qualify_for_architecture(self):
        events = [{"ref": "E000001", "type": "tool.execution_start", "tool": "shell", "arguments": {"command": "test"}, "success": True, "result": "not a completion"}]
        self.assertEqual(inventory(events)["tool_refs"], ["E000001"])
        self.assertEqual(inventory(events)["completion_readback_refs"], [])
        self.assertIn("No completion/readback-eligible references", reference_role_guidance(events))


if __name__ == "__main__":
    unittest.main()
