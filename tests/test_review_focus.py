import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from session_spec.review_focus import SCHEMA, review_focus
from session_spec.story_article import validate_article
from session_spec.story_editor import generate_edition
from session_spec.story_pipeline import run_story
from test_story_pipeline import FakeBackend, article, brief, edition_review, insights, packet


class ReviewFocusTests(unittest.TestCase):
    def test_excerpts_are_exact_and_include_short_and_conflicting_resume_surfaces(self):
        draft = {"article": article(), "brief": brief(), "insights": insights()}
        draft["article"]["title"] = "A compact negative claim"
        draft["article"]["agent_markdown"] += "\n## Another short claim\n"
        original = copy.deepcopy(draft)
        focus = review_focus(draft)
        self.assertEqual(focus["schema"], SCHEMA)
        self.assertIn({"path": "/article/title", "quote": "A compact negative claim"}, focus["short_claims"])
        self.assertIn({"path": "/article/agent_markdown", "quote": "## Another short claim"}, focus["short_claims"])
        for field in ("next_action", "verification_boundary"):
            self.assertTrue(any(item["path"].endswith("/resume/" + field) for item in focus["handoff_conditions"]))
        self.assertTrue(any(item["path"].endswith("/otherwise") for item in focus["handoff_conditions"]))
        self.assertTrue(any(item["path"].endswith("/reuse_condition") for item in focus["handoff_conditions"]))
        self.assertTrue(any(item["path"] == "/brief/approach/text" for item in focus["mechanism_summaries"]))
        self.assertTrue(any(item["path"].startswith("/insights/closing/paragraphs/") for item in focus["mechanism_summaries"]))
        self.assertTrue(any(item["path"].endswith("/recipes/0/when") for item in focus["mechanism_summaries"]))
        for item in focus["short_claims"] + focus["handoff_conditions"] + focus["mechanism_summaries"]:
            value = draft
            for segment in item["path"].split("/")[1:]:
                segment = segment.replace("~1", "/").replace("~0", "~")
                value = value[int(segment)] if isinstance(value, list) else value[segment]
            self.assertIn(item["quote"], value)
        self.assertEqual(draft, original)

    def test_focus_is_saved_with_candidate_hash_and_included_in_actual_review_prompt(self):
        draft = {"article": article(), "brief": brief(), "insights": insights()}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            backend = FakeBackend([edition_review()])
            generate_edition(root, draft, packet(), backend, validate_article)
            focus = json.loads((root / "edition-focus-0.json").read_bytes())
            receipt = json.loads((root / "edition-receipt.json").read_bytes())
            self.assertEqual(focus["candidate_sha256"], receipt["candidate_sha256"])
            self.assertEqual(receipt["identity"]["review_focus"], SCHEMA)
            self.assertIn("REVIEW_FOCUS (deterministic excerpts", backend.prompts[0])
            self.assertIn(json.dumps(review_focus(draft), ensure_ascii=False), backend.prompts[0])
            self.assertIn("不得在另一段自行增加", backend.prompts[0])
            cached = FakeBackend([])
            generate_edition(root, draft, packet(), cached, validate_article)
            self.assertFalse(cached.calls)

    def test_absent_optional_architecture_has_no_invented_claim(self):
        draft = {"article": {}, "insights": {"architecture": {"decision": "omit"}}}
        self.assertEqual(review_focus(draft), {"schema": SCHEMA, "short_claims": [], "handoff_conditions": [], "mechanism_summaries": []})

    def test_story_support_is_not_silently_accepted_as_empty_canonical_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source" / "events.jsonl"
            source.parent.mkdir()
            source.write_bytes(b"synthetic source")
            base = root / "wrong-export"
            base.mkdir()
            (base / "source.json").write_text(json.dumps({"source_path": str(source), "snapshot_bytes": source.stat().st_size,
                                                        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest()}), encoding="utf-8")
            (base / "evidence.jsonl").write_text(json.dumps({"ref": "E000001", "type": "user.message", "text": "No canonical origin field"}), encoding="utf-8")
            backend = FakeBackend([])
            with self.assertRaisesRegex(ValueError, "Canonical export contains no root events"):
                run_story(None, root / "home", root / "output", from_export=base, backend_factory=lambda **settings: backend)
            self.assertFalse(backend.calls)
