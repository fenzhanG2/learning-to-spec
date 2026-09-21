import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from session_spec.agent_package import write_agent_package
from session_spec.storage import file_hash, write_json
from session_spec.story_editor import generate_edition
from session_spec.story_pipeline import run_story, validate_article, validate_story
from session_spec.transfer_probe import effective_feedback
from test_story_pipeline import FakeBackend, edition_review, packet, transfer_review
from test_transfer_probe import draft, finding, resolution, review_with_resolution


def markdown_patch(suffix):
    return {"patches": [{"op": "replace", "path": "/article/agent_markdown",
                         "value": draft()["article"]["agent_markdown"] + suffix}]}


class FinalReaderIntegrityTests(unittest.TestCase):
    def setUp(self):
        renderer = patch("session_spec.story_pipeline.render_story", side_effect=lambda support, target: target.write_bytes(b"<html>synthetic</html>"))
        renderer.start()
        self.addCleanup(renderer.stop)

    def interrupted(self, directory, max_repairs=2, final_responses=None):
        backend = FakeBackend([
            transfer_review(), edition_review([{"reason": "Clarify the bounded state"}]),
            markdown_patch("\n先核对当前状态 [E000002]。"), edition_review(),
            *(final_responses if final_responses is not None else [transfer_review([finding()]), ValueError("synthetic interruption")]),
        ])
        with self.assertRaises(ValueError):
            generate_edition(directory, draft(), packet(), backend, validate_article, transfer_probe=True, max_repairs=max_repairs)
        self.assertFalse((directory / "edition.json").exists())

    def test_resume_preserves_pending_concerns_after_a_later_clean_probe(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.interrupted(directory)
            archived = json.loads((directory / "edition-attempt.json").read_bytes())
            backend = FakeBackend([
                review_with_resolution(status="needs_fix", issues=[{"reason": "Bound the current entry"}]),
                markdown_patch("\n先核对状态与权限 [E000002]。"), review_with_resolution(status="fixed"), transfer_review(),
            ])
            actual = generate_edition(directory, draft(), packet(), backend, validate_article, transfer_probe=True)
            receipt = json.loads((directory / "edition-receipt.json").read_bytes())
            self.assertEqual(len(effective_feedback(receipt, [], packet(), actual)), 1)
            self.assertIn(finding()["risk"], backend.prompts[0])
            self.assertEqual(receipt["review"]["feedback_resolution"][0]["status"], "fixed")
            self.assertEqual(receipt["repair_counts"]["patches"], 2)
            self.assertEqual(len(receipt["followup_transfer_probes"]), 2)
            self.assertEqual(receipt["previous_runs"][-1], archived)

    def test_resumed_new_concern_does_not_mutate_archived_review(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.interrupted(directory)
            archived = json.loads((directory / "edition-attempt.json").read_bytes())
            second = finding()
            second["reason"] = "A distinct reader concern about current permissions"
            final_review = review_with_resolution(status="fixed")
            final_review["feedback_resolution"].append(resolution(index=1))
            backend = FakeBackend([
                review_with_resolution(status="needs_fix", issues=[{"reason": "Bound the current entry"}]),
                markdown_patch("\n先核对状态与权限 [E000002]。"), review_with_resolution(status="fixed"),
                transfer_review([second]), final_review,
            ])
            actual = generate_edition(directory, draft(), packet(), backend, validate_article, transfer_probe=True)
            receipt = json.loads((directory / "edition-receipt.json").read_bytes())
            self.assertEqual(len(effective_feedback(receipt, [], packet(), actual)), 2)
            self.assertEqual(receipt["repair_counts"]["patches"], 2)
            self.assertEqual(len(receipt["review"]["feedback_resolution"]), 2)
            self.assertEqual(receipt["previous_runs"][-1], archived)

    def test_resume_rejects_missing_and_fabricated_dispositions(self):
        fabricated = review_with_resolution()
        fabricated["feedback_resolution"][0]["evidence"][0]["quote"] = "FABRICATED SOURCE SPAN"
        for responses in ([edition_review()], [fabricated, fabricated]):
            with self.subTest(responses=responses), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                self.interrupted(directory)
                with self.assertRaisesRegex(ValueError, "feedback|adjudication"):
                    generate_edition(directory, draft(), packet(), FakeBackend(responses), validate_article, transfer_probe=True)
                self.assertFalse((directory / "edition.json").exists())

    def test_resume_rejects_changed_identity_candidate_and_probe_history(self):
        for mutation in ("model", "candidate", "feedback", "snapshot", "prior_run"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                self.interrupted(directory)
                path = directory / "edition-attempt.json"
                receipt = json.loads(path.read_bytes())
                model = None
                if mutation == "model":
                    model = "different-model"
                elif mutation == "candidate":
                    (directory / "edition-candidate.json").write_bytes(b"{}")
                elif mutation == "feedback":
                    receipt["effective_feedback"] = []
                elif mutation == "snapshot":
                    receipt["followup_transfer_probes"][0]["documents"]["agent-spec.md"] += "tampered"
                else:
                    previous = copy.deepcopy(receipt)
                    previous["effective_feedback"] = []
                    receipt["previous_runs"] = [previous]
                write_json(path, receipt)
                backend = FakeBackend([])
                with self.assertRaises(ValueError):
                    generate_edition(directory, draft(), packet(), backend, validate_article, transfer_probe=True, model=model)
                self.assertEqual(backend.calls, [])

    def test_resume_does_not_replenish_patch_budget(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.interrupted(directory, max_repairs=1)
            backend = FakeBackend([review_with_resolution(status="needs_fix", issues=[{"reason": "Another correction is necessary"}])])
            with self.assertRaisesRegex(ValueError, "bounded review"):
                generate_edition(directory, draft(), packet(), backend, validate_article, transfer_probe=True, max_repairs=1)
            self.assertEqual([item["label"] for item in backend.calls], ["story-edition-review"])
            self.assertFalse((directory / "edition.json").exists())

    def test_failed_probe_records_remain_immutable_and_linked_on_resume(self):
        invalid = transfer_review([finding()])
        invalid["findings"][0]["anchors"][0]["quote"] = "NOT A LITERAL SPAN"
        for phase in ("initial", "final"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                if phase == "final":
                    self.interrupted(directory, final_responses=[invalid, invalid])
                    responses = [edition_review([{"reason": "One more bounded change"}]), markdown_patch("\n先核对权限 [E000002]。"), edition_review(), transfer_review()]
                else:
                    with self.assertRaisesRegex(ValueError, "bounded citation"):
                        generate_edition(directory, draft(), packet(), FakeBackend([invalid, invalid]), validate_article, transfer_probe=True)
                    responses = [transfer_review(), edition_review()]
                previous = json.loads((directory / "edition-attempt.json").read_bytes())
                failed_entry = previous["probe_artifacts"][-1]
                failed_path = directory / failed_entry["path"]
                frozen = failed_path.read_bytes()
                self.assertEqual(json.loads(frozen)["status"], "failed")
                self.assertEqual(len(json.loads(frozen)["attempts"]), 2)
                generate_edition(directory, draft(), packet(), FakeBackend(responses), validate_article, transfer_probe=True)
                receipt = json.loads((directory / "edition-receipt.json").read_bytes())
                self.assertEqual(failed_path.read_bytes(), frozen)
                self.assertIn(failed_entry, receipt["previous_runs"][-1]["probe_artifacts"])
                self.assertTrue(all(entry["path"] != failed_entry["path"] for entry in receipt["probe_artifacts"]))
                self.assertEqual(receipt["effective_feedback"], [])

    def publish(self, root, concerns=False):
        source = root / "source/events.jsonl"
        source.parent.mkdir()
        source.write_bytes(b"synthetic session")
        base = root / "base"
        base.mkdir()
        write_json(base / "source.json", {"source_path": str(source), "snapshot_bytes": source.stat().st_size, "source_sha256": file_hash(source)})
        (base / "evidence.jsonl").write_text("\n".join(json.dumps(event) for event in packet()), encoding="utf-8")
        responses = [draft(), transfer_review([finding()] if concerns else []), review_with_resolution() if concerns else edition_review()]
        output = root / "published"
        run_story(None, root / "copilot", output, from_export=base, backend_factory=lambda **settings: FakeBackend(responses))
        return output

    def rebind(self, output, name, support=False):
        path = output / "_support/story-report.json"
        report = json.loads(path.read_bytes())
        report["support_hashes" if support else "hashes"][name] = file_hash(output / "_support" / name if support else output / name)
        write_json(path, report)

    def test_exact_published_bytes_and_newline_mutations(self):
        for name in ("agent-spec.md", "evidence.md"):
            for newline in (b"\r\n", b"\r"):
                with self.subTest(name=name, newline=newline), tempfile.TemporaryDirectory() as temporary:
                    output = self.publish(Path(temporary))
                    self.assertTrue(validate_story(output)["valid"])
                    receipt = json.loads((output / "_support/edition-receipt.json").read_bytes())
                    for filename in ("agent-spec.md", "evidence.md"):
                        self.assertEqual(file_hash(output / filename), receipt["transfer_probe"]["identity"]["documents"][filename])
                    target = output / name
                    self.assertNotIn(b"\r", target.read_bytes())
                    target.write_bytes(target.read_bytes().replace(b"\n", newline))
                    self.rebind(output, name)
                    self.assertIn("Final delivered pair differs", str(validate_story(output)["issues"]))

    def test_staged_pair_mismatch_refuses_publication(self):
        for name in ("agent-spec.md", "evidence.md"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)

                def altered_package(article, events, language, directory, support):
                    files = write_agent_package(article, events, language, directory, support)
                    target = directory / name
                    target.write_bytes(target.read_bytes() + b"unreviewed staged bytes")
                    return files

                with patch("session_spec.story_pipeline.write_agent_package", side_effect=altered_package):
                    with self.assertRaisesRegex(ValueError, "Final delivered pair differs"):
                        self.publish(root)
                self.assertFalse((root / "published/agent-spec.md").exists())
                self.assertFalse((root / "published/human-spec.html").exists())

    def test_published_dispositions_use_the_bound_original_contract(self):
        for mutation in ("source_quote", "role", "path", "edition_quote", "contract_quote", "snapshot"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temporary:
                output = self.publish(Path(temporary), concerns=True)
                self.assertTrue(validate_story(output)["valid"])
                path = output / "_support/edition-receipt.json"
                receipt = json.loads(path.read_bytes())
                resolution = receipt["review"]["feedback_resolution"][0]
                if mutation == "source_quote":
                    resolution["evidence"][0]["quote"] = "FABRICATED SOURCE SPAN"
                elif mutation == "role":
                    resolution["evidence"][0]["origin"] = "user"
                elif mutation == "path":
                    resolution["path"] = "/article/title"
                elif mutation == "edition_quote":
                    resolution["quote"] = "ABSENT CURRENT EDITION QUOTE"
                elif mutation == "contract_quote":
                    resolution.update(kind="contract", evidence=[], contract_quote="INVENTED CONTRACT RULE")
                else:
                    receipt["review_contract"]["rules"] += "invented scope"
                write_json(path, receipt)
                self.rebind(output, "edition-receipt.json", support=True)
                result = validate_story(output)
                self.assertFalse(result["valid"], result)
                self.assertTrue(any("adjudication" in issue for issue in result["issues"]), result)


if __name__ == "__main__":
    unittest.main()
