import tempfile
import unittest
from pathlib import Path

from session_spec.story_article import validate_article
from session_spec.story_draft import generate_draft
from session_spec.story_editor import generate_edition
from test_story_pipeline import FakeBackend, article, brief, edition_review, insights, packet


class PortableEvidenceContractTests(unittest.TestCase):
    def test_generation_and_review_agree_on_delivered_evidence_boundary(self):
        draft = {"article": article(), "brief": brief(), "insights": insights()}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            generator = FakeBackend([draft])
            generated = generate_draft(root, packet(), None, generator)
            reviewer = FakeBackend([edition_review()])
            generate_edition(root, generated, packet(), reviewer, validate_article)
            for prompt in (generator.prompts[0], reviewer.prompts[0]):
                self.assertIn("`agent-spec.md`、`evidence.md`", prompt)
                self.assertIn("不在交付包中", prompt)
                self.assertIn("关键结论、首步和预期", prompt)
                self.assertNotIn("Agent 可以引用相对证据路径", prompt)
                self.assertNotIn("仅传 MD 时需另附证据附件才能回查", prompt)
            self.assertIn("Do not tell recipients to read, request or transfer private export internals", generator.prompts[0])


if __name__ == "__main__":
    unittest.main()
