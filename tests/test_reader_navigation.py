import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from offline_provider import guard_offline_test
from session_spec.abstract_privacy import READER_NAVIGATION, abstract_surface, render_surface
from session_spec.agent_package import write_agent_package
from session_spec.delivery import deliver, filenames
from session_spec.storage import write_json
from session_spec.story_pipeline import RENDERER, render_story
from test_story_pipeline import article, brief, insights, packet


@unittest.skipUnless(shutil.which("node"), "Actual HTML rendering needs Node.js")
class ReaderNavigationTests(unittest.TestCase):
    def setUp(self):
        guard_offline_test(self)
        # Exercise edited renderer sources before the release bundle is rebuilt.
        source_renderer = patch("session_spec.story_pipeline.renderer_entry", return_value=RENDERER)
        source_renderer.start()
        self.addCleanup(source_renderer.stop)

    def test_selected_delivery_advertises_only_available_companions(self):
        for readers in ("human", "agent", "both"):
            for language in ("en", "zh-CN"):
                with self.subTest(readers=readers, language=language), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    private_story = root / "private"
                    support = private_story / "_support"
                    support.mkdir(parents=True)
                    candidate = article()
                    write_json(support / "edition.json", {"article": candidate, "brief": brief(), "insights": insights()})
                    write_agent_package(candidate, packet(), language, private_story, support)
                    selection = {"readers": readers, "destination": "local"}
                    surface = abstract_surface(private_story, selection)
                    generation = root / "generation"
                    render_surface(surface, {"preferences": selection, "language": language, "reader_navigation": READER_NAVIGATION}, generation / "story")
                    (generation / "reduced").mkdir()
                    write_json(generation / "reduced/reduction.json", {
                        "preferences": selection, "audience": "local", "review_id": "offline-fixture"})
                    result = deliver(generation, selection)
                    self.assertEqual(result["files"], filenames(readers))
                    with zipfile.ZipFile(result["bundle"]) as archive:
                        self.assertEqual(archive.namelist(), filenames(readers))
                        if readers != "agent":
                            html = archive.read("human-spec.html").decode("utf-8")
                            self.assertIn('href="#story-takeaway"', html)
                            if readers == "human":
                                self.assertNotIn("agent-spec.md", html)
                                self.assertNotIn("evidence.md", html)
                                self.assertNotIn("Agent handoff", html)
                                self.assertNotIn("独立交接文件", html)
                                self.assertNotIn("data-open-agent", html)
                            else:
                                self.assertIn("<code>agent-spec.md</code>", html)
                                self.assertIn("<code>evidence.md</code>", html)
                        if readers != "human":
                            for name in ("agent-spec.md", "evidence.md"):
                                self.assertEqual(archive.read(name), (private_story / name).read_bytes())
                            self.assertIn(b"](evidence.md", archive.read("agent-spec.md"))
                            self.assertIn(b"](agent-spec.md)", archive.read("evidence.md"))

    def test_existing_presentation_defaults_keep_the_agent_companion(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_json(root / "article.json", article())
            write_json(root / "insights.json", insights())
            write_json(root / "language.json", {"language": "en"})
            write_json(root / "agent-presentation.json", {"schema": "agent-presentation/v2", "mode": "markdown-files"})
            render_story(root, root / "human-spec.html")
            self.assertIn("<code>agent-spec.md</code>", (root / "human-spec.html").read_text(encoding="utf-8"))

    def test_legacy_approved_human_surface_preserves_071_rendered_bytes(self):
        # SHA captured from the GitHub-installed 0.7.1 renderer using this exact
        # synthetic surface. Legacy approvals bind these bytes, including its footer.
        with tempfile.TemporaryDirectory() as temporary:
            candidate = article()
            candidate.pop("agent_detail", None)
            candidate["agent_markdown"] = ""
            surface = [{"type": "artifact.abstracted", "data": {"human": {
                "article": candidate, "brief": brief(), "insights": insights()}}}]
            manifest = {"preferences": {"readers": "human", "destination": "local"}, "language": "en"}
            hashes = render_surface(surface, manifest, Path(temporary) / "legacy")
            self.assertEqual(hashes, {"human-spec.html": "7aa0a219b36fcbcb7f29fc077b576914ade263cb99c2fa500f1ed0444f3a7910"})
            manifest["reader_navigation"] = READER_NAVIGATION
            self.assertNotEqual(render_surface(surface, manifest, Path(temporary) / "current"), hashes)
            manifest["reader_navigation"] = "unrecognized-policy"
            with self.assertRaisesRegex(ValueError, "Unknown reader navigation policy"):
                render_surface(surface, manifest, Path(temporary) / "invalid")


if __name__ == "__main__":
    unittest.main()
