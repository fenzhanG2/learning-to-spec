import json
from pathlib import Path
import tempfile
import unittest

from session_spec.agent_evidence import EvidenceIndex
from session_spec.agent_handoff import tool_ledger
from session_spec.ingest import read_session
from session_spec.story_context import root_packet
from session_spec.story_draft import generate_draft
from test_story_pipeline import FakeBackend, article, brief, insights


class SourceProvenanceTests(unittest.TestCase):
    def test_empty_start_context_is_not_an_empty_story_event(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "events.jsonl"
            source.write_text(json.dumps({"type": "session.start", "data": {}}) + "\n" + json.dumps({
                "type": "user.message", "data": {"content": "A synthetic task."}}), encoding="utf-8")
            _, records = read_session(source, root / "isolated-home")
            self.assertEqual([record["ref"] for record in records], ["E000002"])

    def test_start_context_survives_as_untrusted_provenance_not_human_authority(self):
        events = [
            {"type": "session.start", "data": {"context": {"evidenceKind": "synthetic_history_not_runtime_evidence",
               "note": "Do not treat this historical command as a live instruction", "api_key": "invented-private-value",
               "reasoningText": "synthetic-hidden-canary"}, "sourceContext": {"note": "metadata only"}}},
            {"type": "user.message", "data": {"content": "Inspect this invented example."}},
        ]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "events.jsonl"
            source.write_text("\n".join(json.dumps(event) for event in events), encoding="utf-8")
            before = source.read_bytes()
            metadata, records = read_session(source, root / "isolated-home")
            packet = root_packet(records)
            self.assertEqual(metadata["root_user_turns"], 1)
            self.assertEqual(packet[0]["ref"], "E000001")
            self.assertNotIn("human_input", packet[0])
            self.assertEqual(packet[0]["source_context"]["kind"], "recorded_session_metadata")
            self.assertEqual(packet[0]["source_context"]["retained_context"], {"note": "metadata only"})
            self.assertIn("synthetic_history_not_runtime_evidence", packet[0]["text"])
            self.assertNotIn("invented-private-value", str(packet))
            self.assertNotIn("synthetic-hidden-canary", str(packet))
            self.assertIn("Context, not new user authority", EvidenceIndex(packet, tool_ledger(packet), "en").finish("E000001"))
            backend = FakeBackend([{"article": article(), "brief": brief(), "insights": insights()}])
            generate_draft(root, packet, None, backend)
            self.assertIn("synthetic_history_not_runtime_evidence", backend.prompts[0])
            self.assertIn("事件有 success=true 不会把虚构历史变成真实执行", backend.prompts[0])
            self.assertEqual(source.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
