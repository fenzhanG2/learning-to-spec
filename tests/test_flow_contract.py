import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from session_spec.storage import PROMPTS
from session_spec.story_draft import generate_draft
from session_spec.story_editor import generate_edition


DRAFT_FLOW = (
    "Preserve consequential control flow in source order: guards, scoped returns/throws, and applicable cleanup or exception paths. "
    "Trace identifiers through recorded assignments and arguments; matching names do not establish shared origin. "
    "Do not describe a later operation as reached when an earlier exit prevents it."
)
REVIEW_FLOW = (
    "For material flow claims, check branch order, exit scope, operation reachability and identifier origin against recorded code. "
    "Report reordered checks, wrongly reachable operations or substituted value origins through existing grounded findings; correct affected counterparts. "
    "An incomplete path or unknown caller limits the claim—it does not license reconstruction."
)
SYNTHETIC_SOURCES = {
    "guarded_origin": (
        "def route_record(record, supplied_key, enabled):\n"
        "    if not enabled:\n"
        "        return None\n"
        "    selected_key = record['stored_key']\n"
        "    return publish_record(selected_key)\n"
    ),
    "scoped_cleanup": (
        "def route_record(record, supplied_key, enabled):\n"
        "    try:\n"
        "        if not enabled:\n"
        "            return None\n"
        "        return publish_record(supplied_key)\n"
        "    finally:\n"
        "        release_record(record)\n"
    ),
    "unknown_caller": (
        "def route_record(supplied_key):\n"
        "    return publish_record(supplied_key)\n"
    ),
}


class PromptCaptured(RuntimeError):
    pass


class CaptureBackend:
    def __init__(self):
        self.calls = []
        self.prompt = None

    def generate(self, prompt, label):
        self.calls.append({"label": label})
        self.prompt = prompt
        raise PromptCaptured("Synthetic prompt capture; no inference or response")


def source_packet(source):
    return [
        {"ref": "E000001", "type": "user.message", "turn": 1,
         "human_input": "Explain only the recorded conditional path and value origin; the caller is not shown."},
        {"ref": "E000002", "type": "tool.execution_complete", "turn": 1,
         "tool": "read_file", "result": {"content": source}, "success": True},
    ]


class FlowContractTests(unittest.TestCase):
    def setUp(self):
        real_backend = patch("session_spec.backend.CopilotBackend.generate", side_effect=AssertionError("Real inference forbidden"))
        real_backend.start()
        self.addCleanup(real_backend.stop)

    def test_additions_are_exact_generic_rules_without_synthetic_identifiers_or_answers(self):
        for filename, marker, expected in (
            ("agent-detail.md", "Preserve consequential control flow", DRAFT_FLOW),
            ("story-editor-method.md", "- For material flow claims", "- " + REVIEW_FLOW),
        ):
            with self.subTest(template=filename):
                text = (PROMPTS / filename).read_text(encoding="utf-8")
                paragraphs = [paragraph for paragraph in text.split("\n\n") if paragraph.startswith(marker)]
                self.assertEqual(paragraphs, [expected])
                self.assertLessEqual(len(expected.split()), 55)
                for identifier in ("route_record", "record['stored_key']", "supplied_key", "publish_record", "release_record"):
                    self.assertNotIn(identifier, expected)
                for source in SYNTHETIC_SOURCES.values():
                    self.assertNotIn(source, expected)

    def test_contract_retains_conditional_exit_scope_and_unknown_origin_boundary(self):
        self.assertIn("scoped returns/throws", DRAFT_FLOW)
        self.assertIn("applicable cleanup or exception paths", DRAFT_FLOW)
        self.assertIn("recorded assignments and arguments", DRAFT_FLOW)
        self.assertIn("when an earlier exit prevents it", DRAFT_FLOW)
        self.assertIn("against recorded code", REVIEW_FLOW)
        self.assertIn("existing grounded findings", REVIEW_FLOW)
        self.assertIn("An incomplete path or unknown caller limits the claim", REVIEW_FLOW)
        self.assertIn("does not license reconstruction", REVIEW_FLOW)

    def test_actual_draft_prompt_includes_rule_and_unchanged_conditional_sources(self):
        for name, source in SYNTHETIC_SOURCES.items():
            with self.subTest(source=name), tempfile.TemporaryDirectory() as temporary:
                packet = source_packet(source)
                original = copy.deepcopy(packet)
                backend = CaptureBackend()
                with self.assertRaises(PromptCaptured):
                    generate_draft(Path(temporary), packet, None, backend, language="en")
                self.assertEqual(backend.calls, [{"label": "story-joint-draft"}])
                self.assertEqual(backend.prompt.count(DRAFT_FLOW), 1)
                self.assertIn(json.dumps(packet, ensure_ascii=False), backend.prompt)
                self.assertLess(backend.prompt.index(DRAFT_FLOW), backend.prompt.index(json.dumps(packet, ensure_ascii=False)))
                self.assertEqual(packet, original)
                self.assertFalse((Path(temporary) / "joint-draft.json").exists())

    def test_actual_editor_prompt_includes_both_rules_and_unchanged_source_not_a_verdict(self):
        for name, source in SYNTHETIC_SOURCES.items():
            with self.subTest(source=name), tempfile.TemporaryDirectory() as temporary:
                packet = source_packet(source)
                edition = {"article": {"agent_markdown": "# Synthetic draft\nConditional behavior needs source review."},
                           "brief": {}, "insights": {}}
                original = copy.deepcopy((packet, edition))
                backend = CaptureBackend()
                with patch("session_spec.story_editor.validate_edition", return_value=[]), self.assertRaises(PromptCaptured):
                    generate_edition(Path(temporary), edition, packet, backend, lambda article, events: [],
                                     language=None, max_repairs=0, max_structural_repairs=0, transfer_probe=False)
                self.assertEqual(backend.calls, [{"label": "story-edition-review"}])
                for rule in (DRAFT_FLOW, REVIEW_FLOW):
                    self.assertEqual(backend.prompt.count(rule), 1)
                    self.assertLess(backend.prompt.index(rule), backend.prompt.index(json.dumps(packet, ensure_ascii=False)))
                self.assertIn(json.dumps(packet, ensure_ascii=False), backend.prompt)
                self.assertIn(json.dumps(edition, ensure_ascii=False), backend.prompt)
                self.assertEqual((packet, edition), original)
                self.assertFalse((Path(temporary) / "edition.json").exists())
                self.assertFalse((Path(temporary) / "edition-receipt.json").exists())


if __name__ == "__main__":
    unittest.main()
