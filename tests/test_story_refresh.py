import hashlib
from contextlib import ExitStack
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from session_spec.agent_handoff import render_agent
from session_spec.agent_package import EVIDENCE_RENDERER, LEGACY_PRESENTATION, PRESENTATION, render_agent_package
from session_spec.cli import parser
from session_spec.pipeline import write_json
from session_spec.storage import file_hash
from session_spec.source_excerpt import payload_text, source_payload
from session_spec.story_pipeline import render_story, run_story, validate_story
from session_spec.story_refresh import refresh_story
from test_story_pipeline import FakeBackend, article, brief, edition_review, insights, packet, transfer_review


@unittest.skipUnless(shutil.which("node"), "Node.js required for HTML rendering")
class StoryRefreshTests(unittest.TestCase):
    def test_complete_and_legacy_published_pairs_validate_without_rebinding(self):
        for renderer in ("companion/v5", "companion/v6", "companion/v7", "companion/v8"):
            with self.subTest(renderer=renderer), tempfile.TemporaryDirectory() as temporary:
                events = packet()
                events[1]["result"]["content"] += "\r\n" + "prefix " * 1000 + "MIDDLE_PAYLOAD" + " suffix" * 1000
                if renderer == "companion/v8":
                    events[0]["human_input"] += "\r\nObservable fixture continuation."
                    events[0]["text"] = events[0]["human_input"]
                output = self.make_story(Path(temporary), final_reader=True, renderer=renderer, events=events)
                self.assertTrue(validate_story(output)["valid"])
                evidence = (output / "evidence.md").read_bytes()
                if renderer == "companion/v8":
                    self.assertIn(payload_text(source_payload(events[1])).encode(), evidence)
                    self.assertIn(events[0]["human_input"].encode(), evidence)
                    self.assertIn(b"\r\n", evidence)
                else:
                    self.assertNotIn(b"MIDDLE_PAYLOAD", evidence)
                report = json.loads((output / "_support/story-report.json").read_bytes())
                self.assertEqual(report["evidence_renderer"], renderer)

    def test_v8_requires_bound_final_probe_and_preserves_refresh_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = self.make_story(root, final_reader=True)
            destination = root / "refreshed"
            refresh_story(output, destination)
            for name in ("agent-spec.md", "evidence.md", "_support/edition-receipt.json"):
                self.assertEqual((output / name).read_bytes(), (destination / name).read_bytes())
            self.assertTrue(validate_story(destination)["valid"])
            support = output / "_support"
            receipt_path = support / "edition-receipt.json"
            receipt = json.loads(receipt_path.read_bytes())
            report_path = support / "story-report.json"
            report = json.loads(report_path.read_bytes())
            for field in ("final_transfer_policy", "transfer_probe", "evidence_renderer"):
                original = receipt["identity"].pop(field)
                write_json(receipt_path, receipt)
                report["support_hashes"]["edition-receipt.json"] = file_hash(receipt_path)
                write_json(report_path, report)
                self.assertIn("v8 final-pair probe binding", str(validate_story(output)["issues"]))
                receipt["identity"][field] = original

    def legacy_companion_files(self, output):
        for name, content in render_agent_package(article(), packet(), "zh-CN", "handoff-split").items():
            (output / name).write_text(content, encoding="utf-8")
            rendered = "agent-rendered.md" if name == "agent-spec.md" else "evidence-rendered.md"
            (output / "_support" / rendered).write_text(content, encoding="utf-8")

    def make_story(self, root, final_reader=False, renderer=None, events=None):
        source = root / "source/events.jsonl"
        source.parent.mkdir()
        source.write_bytes(b"original session")
        base = root / "base"
        base.mkdir()
        write_json(base / "source.json", {"source_path": str(source), "snapshot_bytes": source.stat().st_size,
                                          "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest()})
        (base / "evidence.jsonl").write_text("\n".join(json.dumps(event) for event in (events or packet())), encoding="utf-8")
        backend = FakeBackend([{"article": article(), "brief": brief(), "insights": insights()}, transfer_review(), edition_review()])
        output = root / "published"
        with ExitStack() as stack:
            selected_renderer = renderer or (EVIDENCE_RENDERER if final_reader else "companion/v7")
            if selected_renderer != EVIDENCE_RENDERER:
                for module in ("agent_package", "story_pipeline", "story_editor", "transfer_probe"):
                    stack.enter_context(patch("session_spec." + module + ".EVIDENCE_RENDERER", selected_renderer))
            run_story(None, root / "copilot", output, from_export=base, backend_factory=lambda **settings: backend, language="zh-CN")
        if not final_reader:
            support = output / "_support"
            receipt = json.loads((support / "edition-receipt.json").read_bytes())
            receipt["identity"].pop("final_transfer_policy")
            write_json(support / "edition-receipt.json", receipt)
            report = json.loads((support / "story-report.json").read_bytes())
            report["support_hashes"]["edition-receipt.json"] = file_hash(support / "edition-receipt.json")
            write_json(support / "story-report.json", report)
        return output

    def test_final_reader_binds_delivered_files_not_only_a_fresh_render(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = self.make_story(Path(temporary), final_reader=True)
            self.assertTrue(validate_story(output)["valid"])
            agent = output / "agent-spec.md"
            agent.write_text(agent.read_text(encoding="utf-8") + "\nUnreviewed change", encoding="utf-8")
            report_path = output / "_support/story-report.json"
            report = json.loads(report_path.read_bytes())
            report["hashes"]["agent-spec.md"] = file_hash(agent)
            write_json(report_path, report)
            self.assertIn("Final delivered pair differs", str(validate_story(output)["issues"]))

    def test_refresh_preserves_reviewed_content_evidence_and_original(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = self.make_story(root)
            support = output / "_support"
            report = json.loads((support / "story-report.json").read_bytes())
            report.update(output_schema="story-output/v5", generation_method="actionable-handoff/v2")
            write_json(support / "agent-presentation.json", LEGACY_PRESENTATION)
            legacy_article = article(schema="agent-detail/v2")
            write_json(support / "article.json", legacy_article)
            edition = json.loads((support / "edition.json").read_bytes())
            edition["article"] = legacy_article
            write_json(support / "edition.json", edition)
            receipt = json.loads((support / "edition-receipt.json").read_bytes())
            receipt["output_sha256"] = file_hash(support / "edition.json")
            write_json(support / "edition-receipt.json", receipt)
            expanded = render_agent(legacy_article, packet(), "zh-CN", trajectory_style="expanded")
            (output / "agent-spec.md").write_text(expanded, encoding="utf-8")
            (support / "agent-rendered.md").write_text(expanded, encoding="utf-8")
            render_story(support, output / "human-spec.html")
            report["hashes"] = {name: file_hash(output / name) for name in report["hashes"]}
            report["support_hashes"] = {name: file_hash(support / name) for name in report["support_hashes"]}
            write_json(support / "story-report.json", report)
            self.assertTrue(validate_story(output)["valid"])
            before = {path.relative_to(output): file_hash(path) for path in output.rglob("*") if path.is_file()}
            destination = root / "refreshed"
            result = refresh_story(output, destination)
            self.assertEqual(result["model_calls"], 0)
            self.assertTrue(validate_story(destination)["valid"])
            self.assertTrue(validate_story(output)["valid"])
            self.assertEqual(before, {path.relative_to(output): file_hash(path) for path in output.rglob("*") if path.is_file()})
            for name in ("article.json", "edition.json", "edition-receipt.json", "tool-ledger.json", "evidence.jsonl", "source.json"):
                self.assertEqual(file_hash(support / name), file_hash(destination / "_support" / name))
            new_report = json.loads((destination / "_support/story-report.json").read_bytes())
            self.assertEqual(new_report["output_schema"], "story-output/v6")
            self.assertEqual(new_report["presentation_refresh"]["previous_report_sha256"], file_hash(support / "story-report.json"))
            self.assertEqual(new_report["calls"], [])
            expected = render_agent(legacy_article, packet(), "zh-CN")
            self.assertEqual((destination / "agent-spec.md").read_text(encoding="utf-8"), expected)
            self.assertEqual((destination / "_support/agent-rendered.md").read_text(encoding="utf-8"), expected)
            self.assertNotIn("Readback:", expected)

    def test_refresh_rejects_overlap_existing_and_tampered_sources(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = self.make_story(root)
            for destination in (output, output / "child", root, root / "source/child"):
                with self.assertRaises(ValueError):
                    refresh_story(output, destination)
            occupied = root / "occupied"
            occupied.mkdir()
            (occupied / "keep.md").write_text("user work", encoding="utf-8")
            with self.assertRaises(ValueError):
                refresh_story(output, occupied)
            self.assertEqual((occupied / "keep.md").read_text(encoding="utf-8"), "user work")
            (output / "agent-spec.md").write_text("tampered", encoding="utf-8")
            with self.assertRaises(ValueError):
                refresh_story(output, root / "refused")
            self.assertFalse((root / "refused").exists())

    def test_refresh_cli_has_no_model_or_language_override(self):
        arguments = parser().parse_args(["refresh-story", "original", "--out", "new"])
        self.assertEqual(arguments.directory, Path("original"))
        self.assertEqual(arguments.out, Path("new"))
        self.assertFalse(hasattr(arguments, "model"))
        self.assertFalse(hasattr(arguments, "language"))

    def test_v7_refresh_versions_citation_roles_without_changing_reviewed_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = self.make_story(root)
            support = output / "_support"
            report = json.loads((support / "story-report.json").read_bytes())
            self.assertEqual(report.pop("evidence_renderer"), "companion/v7")
            report["output_schema"] = "story-output/v7"
            write_json(support / "agent-presentation.json", LEGACY_PRESENTATION)
            legacy = render_agent(article(), packet(), "zh-CN", "handoff-legacy")
            (output / "agent-spec.md").write_text(legacy, encoding="utf-8")
            (support / "agent-rendered.md").write_text(legacy, encoding="utf-8")
            render_story(support, output / "human-spec.html")
            report["hashes"] = {name: file_hash(output / name) for name in report["hashes"]}
            report["support_hashes"] = {name: file_hash(support / name) for name in report["support_hashes"]}
            write_json(support / "story-report.json", report)
            self.assertTrue(validate_story(output)["valid"])
            refreshed = root / "refreshed"
            self.assertEqual(refresh_story(output, refreshed)["model_calls"], 0)
            self.assertTrue(validate_story(refreshed)["valid"])
            self.assertTrue(validate_story(output)["valid"])
            current = json.loads((refreshed / "_support/story-report.json").read_bytes())
            self.assertEqual(current["evidence_renderer"], "companion/v7")
            self.assertEqual(current["output_schema"], "story-output/v9")
            for name in ("article.json", "edition.json", "edition-receipt.json", "input.json"):
                self.assertEqual(file_hash(support / name), file_hash(refreshed / "_support" / name))

    def test_companion_is_required_hash_bound_and_checked_against_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = self.make_story(Path(temporary))
            evidence = output / "evidence.md"
            original = evidence.read_text(encoding="utf-8")
            evidence.write_text(original + "\nInvented evidence", encoding="utf-8")
            report_path = output / "_support/story-report.json"
            report = json.loads(report_path.read_bytes())
            report["hashes"]["evidence.md"] = file_hash(evidence)
            write_json(report_path, report)
            self.assertIn("Evidence companion differs", str(validate_story(output)["issues"]))
            evidence.unlink()
            report["hashes"].pop("evidence.md")
            write_json(report_path, report)
            self.assertIn("Required evidence companion", str(validate_story(output)["issues"]))

    def test_markdown_only_handoff_has_no_embedded_content_or_controls(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = self.make_story(Path(temporary))
            html = (output / "human-spec.html").read_text(encoding="utf-8")
            for expected in ('<code>agent-spec.md</code>', '<code>evidence.md</code>', 'href="#mechanism"'):
                self.assertIn(expected, html)
            for forbidden in ('<dialog', '<textarea', '<button', 'data-open-agent', 'id="agent-reader"', 'id="evidence-source"', 'showModal(', 'execCommand(', 'createObjectURL', 'id="e000001"', 'href="agent-spec.md"'):
                self.assertNotIn(forbidden, html)
            self.assertNotIn(article()["agent_detail"]["resume"]["checkpoint"], html)
            self.assertIn('](evidence.md#e000001)', (output / "agent-spec.md").read_text(encoding="utf-8"))
            self.assertIn('### E000001', (output / "evidence.md").read_text(encoding="utf-8"))
            self.assertTrue(validate_story(output)["valid"])

    def test_v8_refresh_preserves_both_markdown_files_and_reviews(self):
        for policy in (None, LEGACY_PRESENTATION):
            with self.subTest(policy=policy), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                output = self.make_story(root)
                support = output / "_support"
                report = json.loads((support / "story-report.json").read_bytes())
                report.update(output_schema="story-output/v8", evidence_renderer="companion/v2" if policy else "companion/v1")
                self.legacy_companion_files(output)
                if policy:
                    write_json(support / "agent-presentation.json", policy)
                else:
                    (support / "agent-presentation.json").unlink()
                    report["support_hashes"].pop("agent-presentation.json")
                render_story(support, output / "human-spec.html")
                report["hashes"] = {name: file_hash(output / name) for name in report["hashes"]}
                report["support_hashes"] = {name: file_hash(support / name) for name in report["support_hashes"]}
                write_json(support / "story-report.json", report)
                self.assertTrue(validate_story(output)["valid"])
                self.assertIn('<dialog', (output / "human-spec.html").read_text(encoding="utf-8"))
                refreshed = root / "refreshed"
                self.assertEqual(refresh_story(output, refreshed)["model_calls"], 0)
                self.assertTrue(validate_story(refreshed)["valid"])
                self.assertTrue(validate_story(output)["valid"])
                for name in ("_support/edition.json", "_support/edition-receipt.json", "_support/input.json"):
                    self.assertEqual(file_hash(output / name), file_hash(refreshed / name))
                for name in ("agent-spec.md", "evidence.md"):
                    self.assertNotEqual(file_hash(output / name), file_hash(refreshed / name))
                self.assertEqual(json.loads((refreshed / "_support/agent-presentation.json").read_bytes()), PRESENTATION)
                self.assertNotIn('<dialog', (refreshed / "human-spec.html").read_text(encoding="utf-8"))
                again = root / "again"
                refresh_story(refreshed, again)
                for name in ("human-spec.html", "agent-spec.md", "evidence.md"):
                    self.assertEqual(file_hash(refreshed / name), file_hash(again / name))

    def test_v9_legacy_companion_validates_and_refreshes_without_new_model_review(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = self.make_story(root)
            support = output / "_support"
            report = json.loads((support / "story-report.json").read_bytes())
            report["evidence_renderer"] = "companion/v2"
            self.legacy_companion_files(output)
            report["hashes"] = {name: file_hash(output / name) for name in report["hashes"]}
            report["support_hashes"] = {name: file_hash(support / name) for name in report["support_hashes"]}
            write_json(support / "story-report.json", report)
            self.assertTrue(validate_story(output)["valid"])
            refreshed = root / "refreshed"
            self.assertEqual(refresh_story(output, refreshed)["model_calls"], 0)
            self.assertTrue(validate_story(output)["valid"])
            self.assertTrue(validate_story(refreshed)["valid"])
            self.assertEqual(file_hash(support / "edition.json"), file_hash(refreshed / "_support/edition.json"))
            self.assertNotIn("_support/", (refreshed / "evidence.md").read_text(encoding="utf-8"))
            report["evidence_renderer"] = "companion/unknown"
            write_json(support / "story-report.json", report)
            self.assertIn("Unknown companion evidence renderer policy", validate_story(output)["issues"])

    def test_markdown_only_policy_cannot_silently_reenable_preview(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = self.make_story(Path(temporary))
            support = output / "_support"
            write_json(support / "agent-presentation.json", LEGACY_PRESENTATION)
            report = json.loads((support / "story-report.json").read_bytes())
            report["support_hashes"]["agent-presentation.json"] = file_hash(support / "agent-presentation.json")
            render_story(support, output / "human-spec.html")
            report["hashes"]["human-spec.html"] = file_hash(output / "human-spec.html")
            write_json(support / "story-report.json", report)
            self.assertIn("Agent presentation policy is missing or invalid", validate_story(output)["issues"])

    def test_legacy_portable_rendering_and_real_heading_remain_reproducible(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = self.make_story(root)
            support = output / "_support"
            self.assertIn("工程会话记录", (output / "human-spec.html").read_text(encoding="utf-8"))
            self.assertNotIn("一段真实的技术工作", (output / "human-spec.html").read_text(encoding="utf-8"))
            report = json.loads((support / "story-report.json").read_bytes())
            report["evidence_renderer"] = "companion/v3"
            for name, content in render_agent_package(article(), packet(), "zh-CN", "handoff-portable").items():
                (output / name).write_text(content, encoding="utf-8")
                rendered = "agent-rendered.md" if name == "agent-spec.md" else "evidence-rendered.md"
                (support / rendered).write_text(content, encoding="utf-8")
            (support / "human-presentation.json").unlink()
            report["support_hashes"].pop("human-presentation.json")
            render_story(support, output / "human-spec.html")
            report["hashes"] = {name: file_hash(output / name) for name in report["hashes"]}
            report["support_hashes"] = {name: file_hash(support / name) for name in report["support_hashes"]}
            write_json(support / "story-report.json", report)
            self.assertTrue(validate_story(output)["valid"])
            self.assertIn("一段真实的技术工作", (output / "human-spec.html").read_text(encoding="utf-8"))
            destination = root / "refreshed"
            refresh_story(output, destination)
            self.assertTrue(validate_story(output)["valid"])
            self.assertTrue(validate_story(destination)["valid"])
            self.assertNotIn("一段真实的技术工作", (destination / "human-spec.html").read_text(encoding="utf-8"))
            self.assertEqual(file_hash(support / "edition.json"), file_hash(destination / "_support/edition.json"))

    def test_v4_companion_and_required_human_policy_remain_reproducible(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = self.make_story(Path(temporary))
            support = output / "_support"
            report = json.loads((support / "story-report.json").read_bytes())
            report["evidence_renderer"] = "companion/v4"
            for name, content in render_agent_package(article(), packet(), "zh-CN", "handoff-portable-v2").items():
                (output / name).write_text(content, encoding="utf-8")
                rendered = "agent-rendered.md" if name == "agent-spec.md" else "evidence-rendered.md"
                (support / rendered).write_text(content, encoding="utf-8")
            report["hashes"] = {name: file_hash(output / name) for name in report["hashes"]}
            report["support_hashes"] = {name: file_hash(support / name) for name in report["support_hashes"]}
            write_json(support / "story-report.json", report)
            self.assertTrue(validate_story(output)["valid"])
            (support / "human-presentation.json").unlink()
            report["support_hashes"].pop("human-presentation.json")
            write_json(support / "story-report.json", report)
            self.assertIn("Human presentation policy is missing or invalid", validate_story(output)["issues"])
