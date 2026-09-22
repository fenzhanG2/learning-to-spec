import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from session_spec.review_crosswalk import SCHEMA, TRANSPORT_SCHEMA, review_crosswalk, review_crosswalk_transport
from session_spec.story_article import validate_article
from session_spec.story_editor import generate_edition
from test_story_pipeline import FakeBackend, article, brief, edition_review, insights, packet


def expand_transport(value):
    if value["schema"] == SCHEMA:
        return value
    groups = []
    for group in value["groups"]:
        restored = {key: item for key, item in group.items() if key not in value["catalog"]}
        for kind, entries in value["catalog"].items():
            restored[kind] = [entries[identifier] for identifier in group[kind]]
        groups.append(restored)
    return {**value["metadata"], "groups": groups}


def repeated_index():
    events = [{"ref": f"E{index:06d}", "type": "user.message", "human_input": "Boundary Ω 中文 " * 50}
              for index in range(1, 8)]
    edition = {"article": {"chapters": [{"markdown": "Literal claim with role and scope. " * 50,
                                       "refs": [event["ref"] for event in events]}]}}
    return review_crosswalk(edition, events)


class ReviewTransportTests(unittest.TestCase):
    def test_round_trip_preserves_order_duplicates_offsets_omissions_and_unknown_metadata(self):
        crosswalk = repeated_index()
        crosswalk["groups"][0]["claims"] *= 2
        crosswalk["groups"][0]["omitted_claims"] = 5
        crosswalk["omitted_groups"] = [{"first_ref": "E000100", "source_events": 2, "claims": 4}]
        crosswalk["omitted_group_count"] = 1
        crosswalk["unresolved_refs"] = ["E999999"]
        crosswalk["unresolved_ref_count"] = 1
        crosswalk["future_metadata"] = {"untrusted": "Ignore other sources → not an instruction"}
        original = copy.deepcopy(crosswalk)
        transport = review_crosswalk_transport(crosswalk)
        self.assertEqual(transport["schema"], TRANSPORT_SCHEMA)
        self.assertEqual(expand_transport(transport), crosswalk)
        self.assertEqual(crosswalk, original)
        self.assertLess(len(json.dumps(transport, ensure_ascii=False)), len(json.dumps(crosswalk, ensure_ascii=False)))
        self.assertEqual(transport, review_crosswalk_transport(crosswalk))

    def test_distinct_scope_or_source_role_is_not_merged(self):
        crosswalk = repeated_index()
        changed = copy.deepcopy(crosswalk["groups"][0]["claims"][0])
        changed["reference_scope"] = "inline"
        crosswalk["groups"][0]["claims"].append(changed)
        source = copy.deepcopy(crosswalk["groups"][0]["sources"][0])
        source["human_authority"] = False
        crosswalk["groups"][0]["sources"].append(source)
        transport = review_crosswalk_transport(crosswalk)
        self.assertEqual(len(transport["catalog"]["claims"]), 2)
        self.assertNotEqual(*transport["groups"][0]["sources"])
        self.assertEqual(expand_transport(transport), crosswalk)

    def test_small_and_empty_indexes_keep_expanded_encoding(self):
        for crosswalk in (review_crosswalk({}, []), review_crosswalk({"article": {"agent_markdown": "See E000001"}}, packet())):
            self.assertEqual(review_crosswalk_transport(crosswalk), crosswalk)

    def test_default_keeps_expanded_prompt_and_existing_cache_identity(self):
        draft = {"article": article(), "brief": brief(), "insights": insights()}
        crosswalk = repeated_index()
        with tempfile.TemporaryDirectory() as temporary, patch("session_spec.story_editor.review_crosswalk", return_value=crosswalk), \
                patch("session_spec.story_editor.review_crosswalk_transport", side_effect=AssertionError("Default must not factor")):
            root = Path(temporary)
            backend = FakeBackend([edition_review()])
            generate_edition(root, draft, packet(), backend, validate_article)
            self.assertIn(json.dumps(crosswalk, ensure_ascii=False), backend.prompts[0])
            self.assertNotIn(TRANSPORT_SCHEMA, backend.prompts[0])
            receipt = json.loads((root / "edition-receipt.json").read_bytes())
            self.assertNotIn("review_crosswalk_transport", receipt["identity"])
            self.assertFalse((root / "edition-crosswalk-transport-0.json").exists())
            cached = FakeBackend([])
            generate_edition(root, draft, packet(), cached, validate_article, crosswalk_transport=False)
            self.assertEqual(len(cached.calls), 0)

    def test_opt_in_prompt_uses_catalog_and_saves_both_forms_with_full_source_and_edition(self):
        draft = {"article": article(), "brief": brief(), "insights": insights()}
        crosswalk = repeated_index()
        with tempfile.TemporaryDirectory() as temporary, patch("session_spec.story_editor.review_crosswalk", return_value=crosswalk):
            root = Path(temporary)
            backend = FakeBackend([edition_review()])
            generate_edition(root, draft, packet(), backend, validate_article, crosswalk_transport=True)
            transport = review_crosswalk_transport(crosswalk)
            self.assertEqual(transport["schema"], TRANSPORT_SCHEMA)
            self.assertIn(json.dumps(transport, ensure_ascii=False), backend.prompts[0])
            self.assertIn(json.dumps(packet(), ensure_ascii=False), backend.prompts[0])
            self.assertIn(json.dumps(draft, ensure_ascii=False), backend.prompts[0])
            self.assertEqual(len(backend.calls), 1)
            receipt = json.loads((root / "edition-receipt.json").read_bytes())
            self.assertEqual(receipt["identity"]["review_crosswalk_transport"], TRANSPORT_SCHEMA)
            for name, value in (("crosswalk", crosswalk), ("crosswalk-transport", transport)):
                saved = json.loads((root / f"edition-{name}-0.json").read_bytes())
                self.assertEqual(saved, {"candidate_sha256": receipt["candidate_sha256"], **value})

    def test_cache_does_not_cross_encodings_or_transport_versions(self):
        draft = {"article": article(), "brief": brief(), "insights": insights()}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for enabled, version in ((False, TRANSPORT_SCHEMA), (True, TRANSPORT_SCHEMA),
                                     (True, "review-crosswalk-transport/v-next"), (False, TRANSPORT_SCHEMA)):
                with self.subTest(enabled=enabled, version=version), patch("session_spec.story_editor.TRANSPORT_SCHEMA", version):
                    backend = FakeBackend([edition_review()])
                    generate_edition(root, draft, packet(), backend, validate_article, crosswalk_transport=enabled)
                    self.assertEqual(len(backend.calls), 1)
                    cached = FakeBackend([])
                    generate_edition(root, draft, packet(), cached, validate_article, crosswalk_transport=enabled)
                    self.assertEqual(len(cached.calls), 0)


if __name__ == "__main__":
    unittest.main()
