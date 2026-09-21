import copy
import json
import tempfile
import unittest
from pathlib import Path

from session_spec.ingest import human_input, make_digest, read_session
from session_spec.story_context import root_packet
from session_spec.story_grounding import quote_basis


class QuestionAnswerTests(unittest.TestCase):
    def record(self, body='"Is zero accepted?"="No, zero must be rejected" user notes: No, zero must be rejected'):
        return {"ref": "E000009", "origin": "root", "type": "tool.execution_complete", "tool": "AskUserQuestion",
                "turn": 1, "line": 9, "text": "", "tool_call_id": "question-one",
                "result": {"content": "User has answered your questions: " + body + ". You can now continue with the user's answers in mind."}}

    def test_only_literal_answers_become_human_not_the_assistant_question(self):
        record = self.record()
        before = copy.deepcopy(record)
        self.assertEqual(human_input(record), "No, zero must be rejected")
        packet = root_packet([record])
        self.assertEqual(packet[0]["human_input"], "No, zero must be rejected")
        self.assertTrue(quote_basis(packet[0], "human", "zero must be rejected"))
        self.assertFalse(quote_basis(packet[0], "human", "Is zero accepted?"))
        self.assertEqual(record, before)

    def test_multiple_answers_and_distinct_notes_preserve_order(self):
        record = self.record('"First?"="Use explicit checks", "Second?"="Do not deploy" user notes: Keep the original failure')
        self.assertEqual(human_input(record), "Use explicit checks\nDo not deploy\nKeep the original failure")

    def test_truncated_error_unknown_tool_or_non_root_result_never_becomes_human(self):
        for mutation in ({"success": False}, {"error": "cancelled"}, {"tool": "Read"}, {"origin": "subagent"}, {"type": "tool.execution_start"}):
            record = {**self.record(), **mutation}
            with self.subTest(mutation=mutation):
                self.assertEqual(human_input(record), "")
        for body in ('"Question"=true', '"Question"=""', 'not a quoted answer', '"Question"="A" followed by unrelated text', '"Question"="A", ', '""="A"'):
            self.assertEqual(human_input(self.record(body)), "")
        record = self.record()
        record["result"]["content"] = record["result"]["content"][:-5]
        self.assertEqual(human_input(record), "")

    def test_imported_copilot_format_keeps_feedback_in_the_digest_and_story(self):
        result = self.record()
        events = [
            {"type": "user.message", "data": {"content": "Repair this synthetic validation."}},
            {"type": "tool.execution_start", "data": {"toolCallId": "question-one", "toolName": "AskUserQuestion", "arguments": {"questions": [{"question": "Is zero accepted?"}]}}},
            {"type": "tool.execution_complete", "data": {"toolCallId": "question-one", "result": result["result"]}},
        ]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "events.jsonl"
            source.write_text("\n".join(json.dumps(event) for event in events), encoding="utf-8")
            _, records = read_session(source, root / "empty-home")
            self.assertEqual(human_input(records[-1]), "No, zero must be rejected")
            digest = make_digest(records, tool_limit=0)
            self.assertIn("No, zero must be rejected", json.dumps(digest))
            self.assertEqual(root_packet(records)[-1]["human_input"], "No, zero must be rejected")


if __name__ == "__main__":
    unittest.main()
