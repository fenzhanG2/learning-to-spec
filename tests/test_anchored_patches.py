import copy
import tempfile
import unittest
from pathlib import Path

from session_spec.patching import apply_data_patches
from session_spec.story_article import validate_article
from session_spec.story_editor import generate_edition
from test_story_pipeline import FakeBackend, article, brief, edition_review, insights, packet


class AnchoredPatchTests(unittest.TestCase):
    def test_local_change_preserves_surrounding_text_and_other_chapters(self):
        spec = {"article": {"chapters": [{"markdown": "Before. Entire output unchanged. After."}, {"markdown": "Closing the task."}]}}
        original = copy.deepcopy(spec)
        result = apply_data_patches(spec, {"patches": [{"op": "replace_text", "path": "/article/chapters/0/markdown", "old": "Entire output unchanged.", "value": "Only the suffix is preserved."}]}, ("article",))
        self.assertEqual(result["article"]["chapters"][0]["markdown"], "Before. Only the suffix is preserved. After.")
        self.assertEqual(result["article"]["chapters"][1], spec["article"]["chapters"][1])
        self.assertEqual(spec, original)

    def test_wrong_chapter_rejects_the_entire_batch(self):
        spec = {"article": {"title": "Original", "chapters": [{"markdown": "Target phrase"}, {"markdown": "Other chapter"}]}}
        original = copy.deepcopy(spec)
        patches = [{"op": "replace", "path": "/article/title", "value": "Changed"},
                   {"op": "replace_text", "path": "/article/chapters/1/markdown", "old": "Target phrase", "value": "Corrected"}]
        with self.assertRaisesRegex(ValueError, "Patch 2.*zero-based path"):
            apply_data_patches(spec, {"patches": patches}, ("article",))
        self.assertEqual(spec, original)

    def test_repeated_and_overlapping_anchors_are_ambiguous(self):
        for text, old in (("same same", "same"), ("aaa", "aa")):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, "ambiguous"):
                apply_data_patches({"article": {"text": text}}, {"patches": [{"op": "replace_text", "path": "/article/text", "old": old, "value": "new"}]}, ("article",))

    def test_types_empty_anchors_and_missing_targets_are_rejected(self):
        for target, old, value in ((4, "4", "new"), ("text", "", "new"), ("text", None, "new"), ("text", "text", {})):
            with self.subTest(target=target, old=old, value=value), self.assertRaises(ValueError):
                apply_data_patches({"article": {"text": target}}, {"patches": [{"op": "replace_text", "path": "/article/text", "old": old, "value": value}]}, ("article",))
        with self.assertRaisesRegex(ValueError, "missing field"):
            apply_data_patches({"article": {}}, {"patches": [{"op": "replace_text", "path": "/article/text", "old": "x", "value": "y"}]}, ("article",))

    def test_exact_whitespace_unicode_and_sequential_operations(self):
        text = "Header\r\nalpha\u20282→beta\r\nFooter"
        patches = [{"op": "replace_text", "path": "/article/paragraphs/0", "old": "alpha\u20282→beta", "value": "new\u2028text"},
                   {"op": "replace_text", "path": "/article/paragraphs/0", "old": "new\u2028text", "value": "final"}]
        result = apply_data_patches({"article": {"paragraphs": [text]}}, {"patches": patches}, ("article",))
        self.assertEqual(result["article"]["paragraphs"][0], "Header\r\nfinal\r\nFooter")

    def test_root_and_operation_restrictions_still_apply(self):
        with self.assertRaisesRegex(ValueError, "canonical spec field"):
            apply_data_patches({"source": "old"}, {"patches": [{"op": "replace_text", "path": "/source", "old": "old", "value": "new"}]}, ("article",))
        with self.assertRaisesRegex(ValueError, "Only add"):
            apply_data_patches({"article": {}}, {"patches": [{"op": "move", "path": "/article"}]}, ("article",))

    def test_editor_exposes_and_applies_anchored_patch(self):
        draft = {"article": article(), "brief": brief(), "insights": insights()}
        title = draft["article"]["title"]
        backend = FakeBackend([edition_review([{"reason": "Correct the title"}]),
                               {"patches": [{"op": "replace_text", "path": "/article/title", "old": title, "value": "静默启动的边界"}]}, edition_review()])
        with tempfile.TemporaryDirectory() as temporary:
            result = generate_edition(Path(temporary), draft, packet(), backend, validate_article)
        self.assertEqual(result["article"]["title"], "静默启动的边界")
        self.assertEqual(draft["article"]["title"], title)
        self.assertIn("replace_text", backend.prompts[1])


if __name__ == "__main__":
    unittest.main()
