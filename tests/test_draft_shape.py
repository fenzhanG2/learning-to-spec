import copy
import json
from pathlib import Path
import tempfile
import unittest

from session_spec.patching import apply_data_patches
from session_spec.storage import PROMPTS, digest
from session_spec.story_draft import generate_draft
from session_spec.story_editor import generate_edition
from session_spec.story_article import validate_article
from test_story_pipeline import FakeBackend, article, brief, edition_review, insights, packet


class DraftShapeTests(unittest.TestCase):
    def test_joint_example_includes_the_required_handoff_and_inline_refs(self):
        template = (PROMPTS / "story-draft.md").read_text(encoding="utf-8")
        example, _ = json.JSONDecoder().raw_decode(template.split("\narticle:\n", 1)[1])
        expected = article()
        self.assertEqual(set(example), set(expected))
        self.assertEqual(set(example["agent_detail"]), set(expected["agent_detail"]))
        self.assertEqual(example["agent_detail"]["schema"], "agent-detail/v3")
        self.assertEqual(set(example["agent_detail"]["resume"]), set(expected["agent_detail"]["resume"]))
        self.assertEqual(set(example["agent_detail"]["trajectory"][0]), set(expected["agent_detail"]["trajectory"][0]))
        self.assertTrue(example["agent_detail"]["trajectory"][0]["tool_steps"][0]["usage"])
        self.assertIn("E000001", example["agent_markdown"])
        with tempfile.TemporaryDirectory() as temporary:
            draft = {"article": expected, "brief": brief(), "insights": insights()}
            backend = FakeBackend([draft])
            generate_draft(Path(temporary), packet(), None, backend)
            self.assertIn("输出前检查 /article/agent_detail", backend.prompts[0])
            receipt = json.loads((Path(temporary) / "joint-draft-receipt.json").read_bytes())
            self.assertEqual(receipt["identity"], digest(("joint-story-draft/v2" + backend.prompts[0] + "copilot-default").encode()))
            generate_draft(Path(temporary), packet(), None, backend)
            self.assertEqual(len(backend.calls), 1)

    def test_missing_field_patch_reports_location_and_is_atomic(self):
        draft = {"article": {"title": "Original", "chapters": [{"title": "Chapter"}]}}
        original = copy.deepcopy(draft)
        patches = [{"op": "replace", "path": "/article/title", "value": "Changed"},
                   {"op": "replace", "path": "/article/chapters/0/refs", "value": ["E000001"]}]
        with self.assertRaisesRegex(ValueError, r"Patch 2.*\/article/chapters/0/refs.*Use add"):
            apply_data_patches(draft, {"patches": patches}, ("article",))
        self.assertEqual(draft, original)
        patches[1]["op"] = "add"
        result = apply_data_patches(draft, {"patches": patches}, ("article",))
        self.assertEqual(result["article"]["chapters"][0]["refs"], ["E000001"])
        self.assertEqual(draft, original)

    def test_missing_parent_and_array_errors_identify_rejected_operation(self):
        with self.assertRaisesRegex(ValueError, r"Patch 1.*\/article/missing/refs.*add the missing container"):
            apply_data_patches({"article": {}}, {"patches": [{"op": "add", "path": "/article/missing/refs", "value": []}]}, ("article",))
        with self.assertRaisesRegex(ValueError, r"Patch 1.*\/article/chapters/2.*out of bounds"):
            apply_data_patches({"article": {"chapters": []}}, {"patches": [{"op": "replace", "path": "/article/chapters/2", "value": {}}]}, ("article",))

    def test_editor_receives_missing_field_recovery_without_auto_creating_content(self):
        draft = {"article": article(), "brief": brief(), "insights": insights()}
        references = draft["article"]["chapters"][0].pop("refs")
        backend = FakeBackend([
            {"patches": [{"op": "replace", "path": "/article/chapters/0/refs", "value": references}]},
            {"patches": [{"op": "add", "path": "/article/chapters/0/refs", "value": references}]},
            edition_review(),
        ])
        with tempfile.TemporaryDirectory() as temporary:
            result = generate_edition(Path(temporary), draft, packet(), backend, validate_article, max_repairs=1, max_structural_repairs=2)
        self.assertEqual(result["article"]["chapters"][0]["refs"], references)
        self.assertNotIn("refs", draft["article"]["chapters"][0])
        self.assertIn("Use add", backend.prompts[1])
        self.assertIn("/article/chapters/0/refs", backend.prompts[1])


if __name__ == "__main__":
    unittest.main()
