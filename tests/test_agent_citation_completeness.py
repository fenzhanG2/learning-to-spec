import copy
import hashlib
import re
import unittest

from session_spec.agent_evidence import EvidenceIndex
from session_spec.agent_handoff import tool_ledger
from session_spec.agent_package import render_agent_package
from test_story_pipeline import article, packet


def installer_corrections():
    candidate = article()
    corrections = [
        ("E000024", "Use the shell installer."),
        ("E000028", "Consolidate installation into a shared step."),
        ("E000032", "Is the matrix running in parallel?"),
        ("E000036", "Install OpenCode using npm -g."),
    ]
    events = packet() + [{"ref": ref, "type": "user.message", "human_input": text, "text": text}
                         for ref, text in corrections]
    phase = candidate["agent_detail"]["trajectory"][0]
    phase["rationale"] = {
        "basis": "recorded",
        "text": "The user requested the shell installer, a shared step, parallelism confirmation, and finally npm global installation.",
        "refs": [ref for ref, _ in corrections],
    }
    return candidate, events


class AgentCitationCompletenessTests(unittest.TestCase):
    def test_fourth_correction_stays_on_claim_and_in_delivered_evidence(self):
        candidate, events = installer_corrections()
        original = copy.deepcopy((candidate, events))
        files = render_agent_package(candidate, events, "en")
        rationale = next(line for line in files["agent-spec.md"].splitlines() if line.startswith("Rationale ["))
        expected = candidate["agent_detail"]["trajectory"][0]["rationale"]["refs"]
        self.assertEqual(re.findall(r"evidence\.md#(e\d{6})", rationale), [ref.lower() for ref in expected])
        for ref in expected:
            self.assertIn("### " + ref, files["evidence.md"])
        self.assertIn("Install OpenCode using npm -g.", files["evidence.md"])
        self.assertEqual((candidate, events), original)

    def test_authored_order_deduplication_and_request_result_support(self):
        events = [
            {"ref": "E000001", "type": "assistant.message", "text": "Reported failure."},
            {"ref": "E000002", "type": "tool.execution_start", "tool_call_id": "edit", "tool": "edit", "arguments": {"path": "file.py"}},
            {"ref": "E000003", "type": "tool.execution_complete", "tool_call_id": "edit", "success": False, "result": "Rejected."},
            {"ref": "E000004", "type": "user.message", "human_input": "Keep the current behavior."},
        ]
        refs = [event["ref"] for event in events]
        authored = refs + [refs[1], refs[0]]
        original = copy.deepcopy((events, authored))
        index = EvidenceIndex(events, tool_ledger(events), "en")
        self.assertEqual(index.cite(authored), "; ".join(refs))
        self.assertEqual((events, authored), original)

    def test_explicit_v8_keeps_the_pre_fix_render_bytes(self):
        candidate, events = installer_corrections()
        files = render_agent_package(candidate, events, "en", evidence_renderer="companion/v8")
        self.assertEqual({name: hashlib.sha256(content.encode()).hexdigest() for name, content in files.items()}, {
            "agent-spec.md": "73719e5c9d78b20ffcd5fdc9fc4ac73a7baff867e7bfdac5476a2eabf5a47998",
            "evidence.md": "d5a9245a4bcd588a5bbfa33376ab1eee78476998bfed546795abff07ab18e45b",
        })
        rationale = next(line for line in files["agent-spec.md"].splitlines() if line.startswith("Rationale ["))
        self.assertNotIn("e000036", rationale)
        self.assertNotIn("### E000036", files["evidence.md"])
