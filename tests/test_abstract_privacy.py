import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from offline_provider import guard_offline_test
from session_spec.abstract_privacy import PIPELINE, generate_abstract_private, load_abstract_review, prepare_abstract_review, validate_abstract_story
from session_spec.privacy import redaction_enabled, sanitize
from session_spec.reduction import load_review, review_identity
from session_spec.storage import write_json
from session_spec.story_pipeline import render_story as actual_render_story
from session_spec.reduction_semantic import semantic_review as actual_semantic_review


SECRET = "ghp_" + "A" * 30
CONTACT = "synthetic.contact@example.test"


class AbstractPrivacyTests(unittest.TestCase):
    def setUp(self):
        guard_offline_test(self)
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source/events.jsonl"
        self.source.parent.mkdir()
        self.source.write_text(json.dumps({"type": "user.message", "data": {
            "content": "Keep failed PATH check. " + CONTACT + " " + SECRET,
            "reasoningText": "HIDDEN_PRIVATE_REASONING"}}) + "\n", encoding="utf-8")
        self.home = self.root / "fixture-home"
        self.review = self.root / "job/review"
        self.output = self.root / "job/generation-test"
        self.order = []
        self.patches = [patch("session_spec.story_pipeline.run_story", side_effect=self.draft),
                        patch("session_spec.story_pipeline.validate_story", side_effect=self.validate_source),
                        patch("session_spec.abstract_privacy.semantic_review", side_effect=self.semantic),
                        patch("session_spec.story_pipeline.render_story", side_effect=self.render)]
        for mocked in self.patches:
            mocked.start()

    def tearDown(self):
        for mocked in reversed(self.patches):
            mocked.stop()
        self.temporary.cleanup()

    def draft(self, session, home, destination, **settings):
        self.order.append("abstract")
        self.assertFalse(redaction_enabled())
        source = (settings["from_export"] / "evidence.jsonl").read_text(encoding="utf-8")
        self.assertIn(SECRET, source)
        self.assertIn(CONTACT, source)
        self.assertNotIn("HIDDEN_PRIVATE_REASONING", source)
        support = destination / "_support"
        support.mkdir(parents=True)
        write_json(support / "edition.json", {"article": {"title": "Failed PATH check", "opening": CONTACT + " " + SECRET,
                                                          "agent_markdown": "NOT_A_HUMAN_SURFACE", "agent_detail": {"hidden": "NOT_A_HUMAN_SURFACE"}},
                                               "brief": {"problem": "PATH remains unverified"}, "insights": {"closing": "Check the actual runner"}})
        write_json(support / "language.json", {"language": "en"})
        write_json(support / "story-report.json", {"fixture": "Mocked source review, not model or repository-test evidence"})
        (destination / "agent-spec.md").write_text("# Agent\nPATH remains unverified. " + CONTACT + " " + SECRET, encoding="utf-8")
        (destination / "evidence.md").write_text("# Evidence\nRecorded failed PATH check. " + CONTACT + " " + SECRET, encoding="utf-8")
        (destination / "human-spec.html").write_text("private fixture", encoding="utf-8")
        return {"status": "mocked_private_draft"}

    def validate_source(self, directory):
        self.assertEqual(directory.name, "story")
        self.assertEqual(directory.parent.name, "abstraction")
        self.assertFalse(redaction_enabled())
        return {"valid": True, "issues": []}

    def semantic(self, directory, **settings):
        self.order.append("redact")
        self.assertTrue(settings["consent"])
        review, baseline = load_review(directory)
        self.assertNotIn(SECRET, json.dumps(baseline))
        self.assertIn(CONTACT, json.dumps(baseline))
        self.assertNotIn("NOT_A_HUMAN_SURFACE", json.dumps(baseline))
        self.assertEqual(review["pipeline"], PIPELINE)
        review["semantic"] = {"status": "reviewed", "provider": "synthetic-test-double", "coverage": "Test fixture only"}
        review["review_id"] = review_identity(review)
        write_json(directory / "review.json", review)
        return review

    def render(self, support, target):
        target.write_text("<html>" + (support / "article.json").read_text(encoding="utf-8") + "</html>", encoding="utf-8")

    def prepare(self, mode="llm", readers="both"):
        return prepare_abstract_review(self.source, self.home, self.review, "local", {"readers": readers, "destination": "local"}, mode)

    def decisions(self, review):
        return {"review_id": review["review_id"], "audience": "local",
                "choices": {finding["id"]: {"action": "pseudonymize"} for finding in review["findings"]}}

    def test_abstraction_sees_complete_observable_content_before_rules_and_semantic(self):
        review = self.prepare()
        self.assertEqual(self.order, ["abstract", "redact"])
        self.assertGreater(review["hard_removals"].get("secret_pattern_matches", 0), 0)
        self.assertFalse(self.output.exists())
        self.assertIn(SECRET, (self.root / "job/abstraction/source/events.jsonl").read_text(encoding="utf-8"))
        self.assertNotIn(SECRET, sanitize(SECRET))

    def test_approved_export_redacts_all_selected_files_without_another_model_call(self):
        review = self.prepare()
        result = generate_abstract_private(self.review, self.decisions(review), self.output, self.home, confirm_choices=True)
        self.assertEqual(self.order, ["abstract", "redact"])
        self.assertEqual(result["model_calls_after_approval"], 0)
        self.assertEqual(set(result["files"]), {"human-spec.html", "agent-spec.md", "evidence.md"})
        for name in result["files"]:
            text = (self.output / "deliverables" / name).read_text(encoding="utf-8")
            self.assertNotIn(SECRET, text)
            self.assertNotIn(CONTACT, text)
            self.assertIn("PATH", text)
        import zipfile

        with zipfile.ZipFile(self.output / "deliverables.zip") as archive:
            self.assertEqual(set(archive.namelist()), set(result["files"]))
        self.assertTrue(validate_abstract_story(self.output / "story")["valid"])

    def test_full_mode_abstracts_first_without_a_privacy_model(self):
        review = self.prepare("full")
        self.assertEqual(self.order, ["abstract"])
        result = generate_abstract_private(self.review, self.decisions(review), self.output, self.home, confirm_choices=True)
        for name in result["files"]:
            self.assertIn(SECRET, (self.output / "deliverables" / name).read_text(encoding="utf-8"))
        self.assertNotIn(SECRET, sanitize(SECRET))

    def test_human_selection_does_not_review_or_deliver_agent_evidence(self):
        review = self.prepare(readers="human")
        unused, baseline = load_review(self.review)
        self.assertEqual(set(baseline[0]["data"]), {"human"})
        result = generate_abstract_private(self.review, self.decisions(review), self.output, self.home, confirm_choices=True)
        self.assertEqual(result["files"], ["human-spec.html"])

    def test_agent_selection_has_no_hidden_human_copy(self):
        review = self.prepare(readers="agent")
        unused, baseline = load_review(self.review)
        self.assertEqual(set(baseline[0]["data"]), {"agent", "evidence"})
        result = generate_abstract_private(self.review, self.decisions(review), self.output, self.home, confirm_choices=True)
        self.assertEqual(set(result["files"]), {"agent-spec.md", "evidence.md"})

    def test_no_approval_missing_choices_or_changed_snapshot_cannot_export(self):
        review = self.prepare()
        decisions = self.decisions(review)
        with self.assertRaisesRegex(ValueError, "approval"):
            generate_abstract_private(self.review, decisions, self.output, self.home)
        with self.assertRaisesRegex(ValueError, "Every finding"):
            generate_abstract_private(self.review, {**decisions, "choices": {}}, self.output, self.home, confirm_choices=True)
        self.source.write_text("{}\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "changed"):
            generate_abstract_private(self.review, decisions, self.output, self.home, confirm_choices=True)
        self.assertFalse((self.output / "deliverables").exists())

    def test_failed_abstraction_never_scans_or_publishes(self):
        with patch("session_spec.story_pipeline.validate_story", return_value={"valid": False, "issues": ["fixture"]}):
            with self.assertRaisesRegex(ValueError, "abstraction failed"):
                self.prepare()
        self.assertEqual(self.order, ["abstract"])
        self.assertFalse(self.review.exists())

    def test_failed_semantic_review_does_not_fall_back_to_rules_only(self):
        with patch("session_spec.abstract_privacy.semantic_review", side_effect=ValueError("synthetic provider failure")):
            with self.assertRaisesRegex(ValueError, "provider failure"):
                self.prepare()
        review, unused = load_review(self.review)
        with self.assertRaisesRegex(ValueError, "completed Copilot review"):
            generate_abstract_private(self.review, self.decisions(review), self.output, self.home, confirm_choices=True)
        self.assertFalse((self.output / "deliverables").exists())

    def test_changed_selected_draft_or_recursive_provenance_is_refused(self):
        review = self.prepare()
        raw = self.root / "job/abstraction/story/agent-spec.md"
        original = raw.read_bytes()
        raw.write_text("Changed", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "surfaces differ"):
            load_abstract_review(self.review, review)
        raw.write_bytes(original)
        write_json(self.root / "job/abstraction/story/_support/privacy-transform.json", {})
        with self.assertRaisesRegex(ValueError, "cannot itself"):
            load_abstract_review(self.review, review, validate_parent=True)

    def test_unchanged_resume_does_not_regenerate_and_tampered_output_is_rejected(self):
        review = self.prepare()
        decisions = self.decisions(review)
        generate_abstract_private(self.review, decisions, self.output, self.home, confirm_choices=True)
        generate_abstract_private(self.review, decisions, self.output, self.home, confirm_choices=True, resume=True)
        self.assertEqual(self.order, ["abstract", "redact"])
        (self.output / "story/evidence.md").write_text("tampered", encoding="utf-8")
        self.assertFalse(validate_abstract_story(self.output / "story")["valid"])

    def test_real_semantic_protocol_keeps_abstraction_binding_and_parent_audit(self):
        from test_story_pipeline import FakeBackend

        backend = FakeBackend([{"findings": [], "limitations": ["Scripted test response, not semantic-quality evidence"]}])
        with patch("session_spec.abstract_privacy.semantic_review", side_effect=lambda directory, **settings:
                   actual_semantic_review(directory, backend=backend, consent=True)):
            review = self.prepare()
        self.assertEqual(review["schema"], "privacy-review/v2")
        self.assertEqual(review["pipeline"], PIPELINE)
        self.assertEqual(load_review(self.review)[0]["abstraction_sha256"], review["abstraction_sha256"])
        generate_abstract_private(self.review, self.decisions(review), self.output, self.home, confirm_choices=True)
        self.assertTrue(validate_abstract_story(self.output / "story")["valid"])
        self.assertEqual(len(backend.calls), 1)

    @unittest.skipUnless(shutil.which("node"), "Actual HTML rendering needs Node.js")
    def test_actual_html_renderer_uses_only_reviewed_human_fields(self):
        from test_story_pipeline import article, brief, insights

        def draft(*arguments, **settings):
            result = self.draft(*arguments, **settings)
            candidate = article()
            candidate["title"] = "PATH remains unverified"
            candidate["subtitle"] = CONTACT + " " + SECRET
            write_json(arguments[2] / "_support/edition.json", {"article": candidate, "brief": brief(), "insights": insights()})
            return result

        with patch("session_spec.story_pipeline.run_story", side_effect=draft), \
                patch("session_spec.story_pipeline.render_story", side_effect=actual_render_story):
            review = self.prepare()
            generate_abstract_private(self.review, self.decisions(review), self.output, self.home, confirm_choices=True)
            self.assertTrue(validate_abstract_story(self.output / "story")["valid"])
        html = (self.output / "deliverables/human-spec.html").read_text(encoding="utf-8")
        self.assertTrue(html.startswith("<!doctype html>"))
        self.assertIn("PATH remains unverified", html)
        self.assertNotIn(SECRET, html)
        self.assertNotIn(CONTACT, html)
        self.assertIn("<svg", html)

    def test_share_package_accepts_only_the_final_redacted_selected_files(self):
        from session_spec.share_package import prepare_package

        review = prepare_abstract_review(self.source, self.home, self.review, "root",
                                         {"readers": "agent", "destination": "artifactstore"}, "llm")
        decisions = {**self.decisions(review), "audience": "root"}
        generate_abstract_private(self.review, decisions, self.output, self.home, confirm_choices=True)
        with patch("session_spec.share_package.validate_story", side_effect=validate_abstract_story):
            manifest = prepare_package(self.output / "story", self.root / "owner-only-package", "root", readers="agent")
        self.assertEqual(set(manifest["files"]), {"agent-spec.md", "evidence.md"})
        self.assertEqual(manifest["audience"], "root")
        for name in manifest["files"]:
            content = (self.root / "owner-only-package/files" / name).read_text(encoding="utf-8")
            self.assertNotIn(SECRET, content)
            self.assertNotIn(CONTACT, content)

    def test_generations_keep_independent_immutable_approval_history(self):
        review = self.prepare()
        first = self.decisions(review)
        second = {**first, "choices": {identifier: {"action": "keep"} for identifier in first["choices"]}}
        other = self.output.with_name("generation-other")
        generate_abstract_private(self.review, first, self.output, self.home, confirm_choices=True)
        generate_abstract_private(self.review, second, other, self.home, confirm_choices=True)
        self.assertTrue(validate_abstract_story(self.output / "story")["valid"])
        self.assertTrue(validate_abstract_story(other / "story")["valid"])
        generate_abstract_private(self.review, first, self.output, self.home, resume=True, confirm_choices=True)
        with self.assertRaisesRegex(ValueError, "choices changed"):
            generate_abstract_private(self.review, second, self.output, self.home, resume=True, confirm_choices=True)
        write_json(self.output / "approved-decisions.json", second)
        self.assertFalse(validate_abstract_story(self.output / "story")["valid"])
        self.assertTrue(validate_abstract_story(other / "story")["valid"])

    def test_resume_refuses_linked_output_before_touching_outside_files(self):
        review = self.prepare()
        decisions = self.decisions(review)
        generate_abstract_private(self.review, decisions, self.output, self.home, confirm_choices=True)
        preserved = (self.output / "deliverables.zip").read_bytes()
        target = self.root / "outside-sentinel.txt"
        target.write_text("UNCHANGED", encoding="utf-8")
        for relative in ("story/human-spec.html", "story/agent-spec.md", "story/evidence.md", "story/_support/article.json.tmp", "delivery.json.tmp"):
            with self.subTest(relative=relative):
                link = self.output / relative
                original = link.read_bytes() if link.exists() else None
                if link.exists():
                    link.unlink()
                try:
                    link.symlink_to(target)
                except OSError:
                    if original is not None:
                        link.write_bytes(original)
                    self.skipTest("Host cannot create synthetic symlink fixtures")
                try:
                    with self.assertRaisesRegex(ValueError, "Linked"):
                        generate_abstract_private(self.review, decisions, self.output, self.home, resume=True, confirm_choices=True)
                    self.assertEqual(target.read_text(encoding="utf-8"), "UNCHANGED")
                    self.assertEqual((self.output / "deliverables.zip").read_bytes(), preserved)
                finally:
                    link.unlink()
                    if original is not None:
                        link.write_bytes(original)

    def test_job_local_review_locator_rejects_traversal(self):
        review = self.prepare()
        generate_abstract_private(self.review, self.decisions(review), self.output, self.home, confirm_choices=True)
        receipt_path = self.output / "story/_support/privacy-transform.json"
        receipt = json.loads(receipt_path.read_bytes())
        write_json(receipt_path, {**receipt, "review_directory": "../review"})
        self.assertFalse(validate_abstract_story(self.output / "story")["valid"])


if __name__ == "__main__":
    unittest.main()
