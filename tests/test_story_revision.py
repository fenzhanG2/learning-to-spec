import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from session_spec.storage import file_hash, write_json
from session_spec.story_pipeline import run_story, validate_story
from session_spec.story_revision import load_revision
from session_spec.story_context import root_packet
from test_story_pipeline import FakeBackend, article, brief, edition_review, insights, packet, transfer_review


class StoryRevisionTests(unittest.TestCase):
    def seed(self, root):
        work = root / "prior/_support/.work"
        work.mkdir(parents=True)
        write_json(work / "source.json", {"source_sha256": "source"})
        write_json(work / "input.json", packet())
        draft = {"article": article(), "brief": brief(), "insights": insights()}
        write_json(work / "edition-candidate.json", draft)
        write_json(work / "edition-attempt.json", {"status": "failed", "candidate_sha256": file_hash(work / "edition-candidate.json")})
        return work, draft

    def test_prior_candidate_is_starting_material_not_inherited_approval(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            work, draft = self.seed(root)
            value, origin = load_revision(root / "prior", packet(), {"source_sha256": "source"})
            self.assertEqual(value, draft)
            self.assertEqual(origin["kind"], "unapproved_candidate")
            self.assertIn("Approval is not inherited", origin["note"])

    def test_source_interpretation_and_content_hash_cannot_drift(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            work, draft = self.seed(root)
            with self.assertRaises(ValueError):
                load_revision(root / "prior", packet(), {"source_sha256": "other"})
            with self.assertRaises(ValueError):
                load_revision(root / "prior", packet()[:-1], {"source_sha256": "source"})
            write_json(work / "edition-candidate.json", {"tampered": True})
            with self.assertRaises(ValueError):
                load_revision(root / "prior", packet(), {"source_sha256": "source"})

    @unittest.skipUnless(shutil.which("node"), "Node.js required for HTML rendering")
    def test_revision_reviews_again_without_a_new_joint_draft(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = root / "source/events.jsonl"
            original.parent.mkdir()
            original.write_bytes(b"historical session")
            source = {"source_path": str(original), "source_sha256": hashlib.sha256(original.read_bytes()).hexdigest(), "snapshot_bytes": original.stat().st_size}
            base = root / "base"
            base.mkdir()
            write_json(base / "source.json", source)
            (base / "evidence.jsonl").write_text("\n".join(json.dumps(event) for event in packet()), encoding="utf-8")
            work, draft = self.seed(root)
            write_json(work / "source.json", source)
            write_json(work / "input.json", root_packet(packet()))
            backend = FakeBackend([transfer_review(), edition_review()])
            output = root / "revised"
            result = run_story(None, root / "copilot", output, from_export=base, revise_from=root / "prior", backend_factory=lambda **settings: backend)
            self.assertEqual(result["model_calls"], 2)
            self.assertEqual(backend.calls[0]["label"], "story-transfer-probe")
            self.assertEqual(backend.calls[1]["label"], "story-edition-review")
            self.assertTrue(validate_story(output)["valid"])
            self.assertEqual(json.loads((output / "_support/edition.json").read_bytes()), draft)
            origin = json.loads((output / "_support/draft-origin.json").read_bytes())
            self.assertEqual(origin["method"], "source_checked_revision")
            self.assertIn("draft-origin.json", json.loads((output / "_support/story-report.json").read_bytes())["support_hashes"])
