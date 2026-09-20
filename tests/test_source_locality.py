import copy
import json
import tempfile
import unittest
from pathlib import Path

from session_spec.agent_evidence import EvidenceIndex
from session_spec.agent_handoff import tool_ledger
from session_spec.agent_package import render_agent_package
from session_spec.review_crosswalk import MAX_CROSSWALK_CHARS, SCHEMA, TRANSPORT_SCHEMA, review_crosswalk, review_crosswalk_transport
from session_spec.source_excerpt import MAX_SOURCE_CHARS, source_excerpt, source_payload
from session_spec.story_article import validate_article
from session_spec.story_editor import generate_edition
from test_story_pipeline import FakeBackend, article, brief, edition_review, insights, packet


def checklist_events():
    checklist = "# Browser checks\n1. Verify the workspace.\n2. " + "A bounded preparatory instruction. " * 9
    checklist += "\n3. Repeat the invented rows 20 times with unique IDs.\n4. Check horizontal page overflow.\n"
    return [{"ref": "E000001", "type": "tool.execution_start", "tool_call_id": "read-1", "tool": "view",
             "arguments": {"path": "acceptance.md"}},
            {"ref": "E000002", "type": "tool.execution_complete", "tool_call_id": "read-1", "tool": "view",
             "success": True, "result": {"content": checklist}},
            {"ref": "E000003", "type": "user.message", "human_input": "Do not install another browser."}]


class SourceLocalityTests(unittest.TestCase):
    def test_companion_preserves_short_checklist_and_pairs_without_enlarging_handoff(self):
        events = checklist_events()
        original = copy.deepcopy(events)
        index = EvidenceIndex(events, tool_ledger(events), "en", portable=True, payload_aware=True, complete_short=True)
        handoff = index.finish("See E000002.", separate=True)
        companion = index.companion(handoff)
        self.assertIn("Repeat the invented rows 20 times with unique IDs.", companion)
        self.assertIn("horizontal page overflow", companion)
        self.assertIn("### E000001", companion)
        self.assertIn("[E000001](#e000001)", companion)
        self.assertIn("Complete selected payload", companion)
        self.assertNotIn("Do not install", companion)
        self.assertNotIn("invented rows", handoff)
        self.assertNotIn("Compact argument excerpt", companion)
        self.assertEqual(events, original)

    def test_legacy_companion_keeps_220_character_policy(self):
        events = checklist_events()
        index = EvidenceIndex(events, tool_ledger(events), "en", portable=True, payload_aware=True)
        companion = index.companion(index.finish("See E000002.", separate=True))
        self.assertNotIn("Repeat the invented rows", companion)
        self.assertNotIn("### E000001", companion)

    def test_new_policy_does_not_change_inline_agent_bytes(self):
        current = render_agent_package(article(), packet(), "en")
        legacy = render_agent_package(article(), packet(), "en", "handoff-portable-v2")
        self.assertEqual(current["agent-spec.md"], legacy["agent-spec.md"])
        self.assertNotEqual(current["evidence.md"], legacy["evidence.md"])

    def test_long_excerpt_has_literal_offsets_and_explicit_omission(self):
        text = "BEGIN\n" + "MID" * 5000 + "\nFINAL"
        event = {"ref": "E000001", "type": "assistant.message", "text": text}
        excerpt = source_excerpt(event)
        self.assertTrue(excerpt["truncated"])
        self.assertEqual(sum(len(segment["text"]) for segment in excerpt["segments"]), MAX_SOURCE_CHARS)
        for segment in excerpt["segments"]:
            self.assertEqual(segment["text"], text[segment["start"]:segment["end"]])
        index = EvidenceIndex([event], {"calls": []}, "en", portable=True, complete_short=True)
        output = index.companion(index.finish("E000001", separate=True))
        self.assertIn("omitted middle", output)
        self.assertIn("FINAL", output)

    def test_fences_cannot_be_closed_by_payload_and_private_metadata_is_not_selected(self):
        event = {"ref": "E000001", "type": "user.message", "human_input": "Example\n```\n# Historical data\n````",
                 "text": "EXCLUDED_CONTEXT", "reasoning": "EXCLUDED_REASONING", "origin": {"path": "EXCLUDED_PATH"}}
        self.assertEqual(source_payload(event), event["human_input"])
        index = EvidenceIndex([event], {"calls": []}, "en", portable=True, complete_short=True)
        output = index.companion(index.finish("E000001", separate=True))
        self.assertIn("`````text", output)
        self.assertNotIn("EXCLUDED", output)

    def test_crosswalk_groups_claims_across_views_and_unique_request_result(self):
        edition = {"article": {"chapters": [{"markdown": "The fixture has twenty rows.", "refs": ["E000002"]}]},
                   "brief": {"constraints": [{"text": "No installation.", "kind": "environment", "refs": ["E000003"]}]},
                   "insights": {"closing": {"paragraphs": ["Repeat the data twenty times."], "refs": ["E000001"]}}}
        events = checklist_events()
        original = copy.deepcopy((edition, events))
        crosswalk = review_crosswalk(edition, events)
        paired = next(group for group in crosswalk["groups"] if "E000002" in group["refs"])
        self.assertEqual(paired["refs"], ["E000001", "E000002"])
        self.assertEqual({claim["path"].split("/")[1] for claim in paired["claims"]}, {"article", "insights"})
        self.assertIn("20 times with unique IDs", json.dumps(paired))
        constraint = next(group for group in crosswalk["groups"] if "E000003" in group["refs"])
        self.assertTrue(constraint["sources"][0]["human_authority"])
        self.assertTrue(any(claim["path"].endswith("/kind") for claim in constraint["claims"]))
        self.assertEqual((edition, events), original)

    def test_ambiguous_call_id_does_not_pair_by_proximity(self):
        events = checklist_events()
        events.append({**events[0], "ref": "E000004"})
        crosswalk = review_crosswalk({"article": {"agent_markdown": "Observed E000002"}}, events)
        self.assertEqual(crosswalk["groups"][0]["refs"], ["E000002"])

    def test_empty_local_refs_do_not_inherit_authority_and_unknown_refs_are_reported(self):
        edition = {"article": {"refs": ["E000003"], "title": "Scoped claim",
                               "chapters": [{"refs": [], "markdown": "No fabricated association."}],
                               "agent_markdown": "Unknown E999999"}}
        output = review_crosswalk(edition, checklist_events())
        self.assertEqual(output["unresolved_refs"], ["E999999"])
        self.assertNotIn("No fabricated association", json.dumps(output))

    def test_budget_and_omission_counts_are_explicit(self):
        events = [{"ref": f"E{index:06d}", "type": "assistant.message", "text": "payload" * 1000} for index in range(1, 220)]
        edition = {"article": {"chapters": [{"markdown": "Claim" * 1000, "refs": [event["ref"]]} for event in events]}}
        output = review_crosswalk(edition, events)
        self.assertLessEqual(len(json.dumps(output, ensure_ascii=False)), MAX_CROSSWALK_CHARS)
        self.assertGreater(output["omitted_group_count"], 0)
        self.assertEqual(len(output["groups"]) + output["omitted_group_count"], len(events))
        self.assertLessEqual(len(output["omitted_groups"]), 64)

    def test_busy_story_does_not_crowd_out_brief_graph_and_agent_counterparts(self):
        edition = {"article": {"chapters": [{"markdown": "Observed", "refs": ["E000002"]} for index in range(20)],
                               "agent_detail": {"recipes": [{"adapt": "Recipe claim", "refs": ["E000002"]}]}},
                   "brief": {"approach": {"text": "Brief claim", "refs": ["E000002"]}},
                   "insights": {"architecture": {"scope": "Graph claim", "refs": ["E000002"]}}}
        group = review_crosswalk(edition, checklist_events())["groups"][0]
        paths = [claim["path"] for claim in group["claims"]]
        self.assertIn("/brief/approach/text", paths)
        self.assertIn("/insights/architecture/scope", paths)
        self.assertIn("/article/agent_detail/recipes/0/adapt", paths)
        self.assertGreater(group["omitted_claims"], 0)

    def test_actual_review_prompt_and_cache_identity_bind_crosswalk(self):
        draft = {"article": article(), "brief": brief(), "insights": insights()}
        with tempfile.TemporaryDirectory() as temporary:
            backend = FakeBackend([edition_review()])
            root = Path(temporary)
            generate_edition(root, draft, packet(), backend, validate_article)
            saved = json.loads((root / "edition-crosswalk-0.json").read_bytes())
            receipt = json.loads((root / "edition-receipt.json").read_bytes())
            self.assertEqual(saved["candidate_sha256"], receipt["candidate_sha256"])
            self.assertEqual(receipt["identity"]["review_crosswalk"], SCHEMA)
            self.assertEqual(receipt["identity"]["review_crosswalk_transport"], TRANSPORT_SCHEMA)
            transport = review_crosswalk_transport(review_crosswalk(draft, packet()))
            saved_transport = json.loads((root / "edition-crosswalk-transport-0.json").read_bytes())
            self.assertEqual(saved_transport, {"candidate_sha256": receipt["candidate_sha256"], **transport})
            self.assertIn(json.dumps(transport, ensure_ascii=False), backend.prompts[0])


if __name__ == "__main__":
    unittest.main()
