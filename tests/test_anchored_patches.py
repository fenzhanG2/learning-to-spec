import copy
import json
import tempfile
import unittest
from pathlib import Path

from session_spec.patching import apply_data_patches
from session_spec.story_article import validate_article
from session_spec.story_editor import generate_edition
from test_story_pipeline import FakeBackend, article, brief, edition_review, insights, packet


class AnchoredPatchTests(unittest.TestCase):
    def rejected_diagnostic(self, spec, patches, roots=("article",)):
        original = copy.deepcopy(spec)
        with self.assertRaisesRegex(ValueError, "No changes applied.*Navigation only") as failure:
            apply_data_patches(spec, {"patches": patches}, roots)
        self.assertEqual(spec, original)
        return json.loads(str(failure.exception).split("Anchor diagnostic: ", 1)[1])

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

    def test_structured_text_diagnostic_uses_original_not_partially_patched_candidate(self):
        path = "/article/agent_detail/trajectory/0/tool_steps/0/purpose"
        for field in ("purpose", "finding"):
            with self.subTest(field=field):
                original_text = "Inspect the declared entry point before choosing a change."
                spec = {"article": {"agent_markdown": "Base handoff with separate structured detail.",
                                   "agent_detail": {"trajectory": [{"tool_steps": [{field: original_text}]}]}}}
                field_path = path.rsplit("/", 1)[0] + "/" + field
                patches = [{"op": "replace_text", "path": field_path, "old": original_text, "value": "Inspect current state first."},
                           {"op": "replace_text", "path": "/article/agent_markdown", "old": "declared entry point", "value": "current state"}]
                diagnostic = self.rejected_diagnostic(spec, patches)
                self.assertEqual(diagnostic["basis"], "original_before_batch")
                self.assertEqual(diagnostic["target"]["text"], spec["article"]["agent_markdown"])
                self.assertEqual([match["path"] for match in diagnostic["exact_matches"]], [field_path])
                self.assertEqual(diagnostic["exact_matches"][0]["excerpt"]["text"], original_text)
                self.assertTrue(diagnostic["search_complete"])

    def test_diagnostic_is_exact_root_scoped_and_escapes_json_pointers(self):
        spec = {"source": {"private": "needle OUTSIDE_ROOT_CANARY"},
                "article": {"target": "NEEDLE and two\u00a0spaces", "a/b~c": ["needle inside"]}}
        diagnostic = self.rejected_diagnostic(spec, [{"op": "replace_text", "path": "/article/target", "old": "needle", "value": "new"}])
        self.assertEqual([match["path"] for match in diagnostic["exact_matches"]], ["/article/a~1b~0c/0"])
        self.assertNotIn("OUTSIDE_ROOT_CANARY", json.dumps(diagnostic))
        for old in ("two spaces", "Needle", "needle\r\ninside"):
            with self.subTest(old=old):
                diagnostic = self.rejected_diagnostic(spec, [{"op": "replace_text", "path": "/article/target", "old": old, "value": "new"}])
                self.assertEqual(diagnostic["exact_matches"], [])
                self.assertTrue(diagnostic["search_complete"])

    def test_diagnostic_excerpts_and_match_list_are_bounded_and_deterministic(self):
        spec = {"article": {"target": "x" * 500, "fields": ["prefix needle " + "y" * 500 for _ in range(8)],
                            "p" * 300: "needle"}}
        patches = [{"op": "replace_text", "path": "/article/target", "old": "needle", "value": "new"}]
        diagnostic = self.rejected_diagnostic(spec, patches)
        self.assertEqual(diagnostic, self.rejected_diagnostic(spec, patches))
        self.assertEqual(len(diagnostic["target"]["text"]), 160)
        self.assertTrue(diagnostic["target"]["truncated"])
        self.assertEqual(len(diagnostic["exact_matches"]), 4)
        self.assertTrue(diagnostic["matches_omitted"])
        self.assertTrue(diagnostic["search_complete"])
        self.assertTrue(all(len(match["excerpt"]["text"]) <= 160 for match in diagnostic["exact_matches"]))
        diagnostic = self.rejected_diagnostic({"article": {"target": "base", "p" * 300: "needle"}}, patches)
        self.assertEqual(len(diagnostic["exact_matches"][0]["path"]), 240)
        self.assertTrue(diagnostic["exact_matches"][0]["path_truncated"])

    def test_diagnostic_reports_incomplete_search_at_each_scan_bound(self):
        deep = "needle"
        for _ in range(40):
            deep = {"child": deep}
        targets = (["unused"] * 600 + ["needle"], "x" * 20000 + "needle", deep)
        for hidden in targets:
            with self.subTest(kind=type(hidden).__name__):
                diagnostic = self.rejected_diagnostic({"article": {"target": "base", "hidden": hidden}},
                    [{"op": "replace_text", "path": "/article/target", "old": "needle", "value": "new"}])
                self.assertFalse(diagnostic["search_complete"])
                self.assertEqual(diagnostic["exact_matches"], [])
                self.assertLessEqual(diagnostic["nodes_visited"], 512)

    def test_diagnostic_handles_targets_created_or_retyped_in_rejected_batch(self):
        for article_value in ({}, {"new": 7}):
            with self.subTest(article=article_value):
                diagnostic = self.rejected_diagnostic({"article": article_value},
                    [{"op": "add", "path": "/article/new", "value": "base"},
                     {"op": "replace_text", "path": "/article/new", "old": "needle", "value": "new"}])
                self.assertIn(diagnostic["target"]["state"], {"missing_before_batch", "not_a_string_before_batch"})
                self.assertEqual(diagnostic["exact_matches"], [])

    def test_original_target_resolution_retains_strict_array_indices_and_literal_object_keys(self):
        for key in ("-1", "01", "+0", " 0"):
            with self.subTest(key=key):
                spec = {"article": {"branch": ["original zero", "original one"]}}
                diagnostic = self.rejected_diagnostic(spec, [
                    {"op": "replace", "path": "/article/branch", "value": {key: "base"}},
                    {"op": "replace_text", "path": "/article/branch/" + key, "old": "needle", "value": "new"}])
                self.assertEqual(diagnostic["target"], {"state": "missing_before_batch"})
        diagnostic = self.rejected_diagnostic({"article": {"branch": {"01": "literal object key"}}},
            [{"op": "replace_text", "path": "/article/branch/01", "old": "needle", "value": "new"}])
        self.assertEqual(diagnostic["target"]["text"], "literal object key")

    def test_long_key_descendants_keep_a_bounded_display_path_prefix(self):
        spec = {"article": {"target": "base", "~/" * 32768: {"child": "needle"}}}
        diagnostic = self.rejected_diagnostic(spec, [{"op": "replace_text", "path": "/article/target", "old": "needle", "value": "new"}])
        match = diagnostic["exact_matches"][0]
        expected = ("/article/" + "~0~1" * 32768 + "/child")[:240]
        self.assertEqual(match["path"], expected)
        self.assertTrue(match["path_truncated"])

    def test_editor_passes_rejection_diagnostic_to_next_bounded_repair(self):
        draft = {"article": article(), "brief": brief(), "insights": insights()}
        original = copy.deepcopy(draft)
        path = "/article/agent_detail/trajectory/0/tool_steps/0/purpose"
        edit = {"op": "replace_text", "path": path, "old": "确认入口。", "value": "先读回当前入口。"}
        wrong = {"op": "replace_text", "path": "/article/agent_markdown", "old": "确认入口", "value": "读回当前入口"}
        backend = FakeBackend([edition_review([{"reason": "Clarify the inspection purpose", "path": path, "quote": "确认入口。"}]),
                               {"patches": [edit, wrong]}, {"patches": [edit]}, edition_review()])
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            result = generate_edition(directory, draft, packet(), backend, validate_article, max_repairs=2)
            receipt = json.loads((directory / "edition-receipt.json").read_bytes())
        self.assertEqual(result["article"]["agent_detail"]["trajectory"][0]["tool_steps"][0]["purpose"], edit["value"])
        self.assertEqual(result["article"]["agent_markdown"], draft["article"]["agent_markdown"])
        self.assertEqual(draft, original)
        self.assertEqual(len(backend.calls), 4)
        self.assertEqual(receipt["repair_counts"]["patches"], 2)
        self.assertIn("original_before_batch", backend.prompts[2])
        self.assertIn(path, backend.prompts[2])
        self.assertTrue(any("Anchor diagnostic:" in attempt.get("patch_error", "") for attempt in receipt["attempts"]))

    def test_repeated_bad_anchor_does_not_extend_editor_patch_budget(self):
        draft = {"article": article(), "brief": brief(), "insights": insights()}
        rejected = {"patches": [{"op": "replace_text", "path": "/article/agent_markdown", "old": "确认入口", "value": "读回当前入口"}]}
        backend = FakeBackend([edition_review([{"reason": "Clarify the inspection purpose"}]), rejected, rejected])
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            with self.assertRaisesRegex(ValueError, "failed bounded review"):
                generate_edition(directory, draft, packet(), backend, validate_article, max_repairs=2)
            receipt = json.loads((directory / "edition-attempt.json").read_bytes())
            self.assertFalse((directory / "edition.json").exists())
            self.assertEqual(json.loads((directory / "edition-candidate.json").read_bytes()), draft)
        self.assertEqual(len(backend.calls), 3)
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(receipt["repair_counts"]["patches"], 2)
        self.assertEqual(sum("patch_error" in attempt for attempt in receipt["attempts"]), 2)

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
        self.assertIn("/article/agent_markdown 基础 Markdown 加上 /article/agent_detail", backend.prompts[1])
        self.assertIn("不代表它也在 agent_markdown 中", backend.prompts[1])
        self.assertIn("不是重定位授权或语义正确性证明", backend.prompts[1])


if __name__ == "__main__":
    unittest.main()
