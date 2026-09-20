import copy
import json
import tempfile
import unittest
from pathlib import Path

from session_spec.assertion_scope import MAX_ASSERTIONS, MAX_INDEX_CHARS, MAX_PAYLOAD_CHARS, SCHEMA, assertion_scope
from session_spec.story_article import validate_article
from session_spec.story_editor import generate_edition
from session_spec.source_excerpt import read_display_text
from session_spec.story_grounding import quote_basis
from test_story_pipeline import FakeBackend, article, brief, edition_review, insights, packet


def source(text, ref="E000003", tool="Read"):
    return {"ref": ref, "type": "tool.execution_complete", "tool": tool, "result": {"content": text}, "success": True}


class AssertionScopeTests(unittest.TestCase):
    def test_suffix_is_not_identity_and_examples_are_hypothetical(self):
        text = "assert redact('sample', 0, 0).endswith('sample')"
        events = [source(text)]
        edition = {"article": {"chapters": [{"markdown": "Empty spans leave the whole message untouched.", "refs": ["E000003"]}],
                               "agent_markdown": "The original suffix remains, E000003."}}
        original = copy.deepcopy((edition, events))
        output = assertion_scope(edition, events)
        row = output["assertions"][0]
        self.assertEqual(row["quote"], text)
        self.assertEqual(row["relation"], "suffix predicate")
        self.assertEqual(row["hypothetical_builtin_string_example"], {"observed": "prefix_sample", "expected": "sample", "predicate_holds": True, "equal": False, "historical_execution": False})
        self.assertEqual({claim["path"] for claim in row["claims"]}, {"/article/chapters/0/markdown", "/article/agent_markdown"})
        self.assertEqual((edition, events), original)

    def test_prefix_membership_and_equality_keep_different_claim_strength(self):
        text = "assert result.startswith('sample')\nassert 'sample' in result\nassert result == expected"
        output = assertion_scope({}, [source(text)])
        self.assertEqual([row["relation"] for row in output["assertions"]], ["prefix predicate", "membership predicate", "equality predicate"])
        self.assertNotIn("hypothetical_builtin_string_example", output["assertions"][2])

    def test_later_trajectory_and_recipe_counterparts_are_not_omitted_after_four_surfaces(self):
        ref = ["E000003"]
        edition = {"article": {"chapters": [{"markdown": "Human claim", "refs": ref}], "agent_markdown": "Mechanism E000003",
                               "checks": [{"observed": "Check claim", "refs": ref}], "agent_detail": {
                                   "resume": {"checkpoint": "Resume claim", "refs": ref},
                                   "continuation": [{"action": "Continue claim", "refs": ref}],
                                   "recipes": [{"adapt": "Recipe claim", "refs": ref}],
                                   "trajectory": [{"observation": "Contradictory trajectory", "refs": ref}]}}}
        output = assertion_scope(edition, [source("assert result.endswith('sample')")])
        paths = {claim["path"] for claim in output["assertions"][0]["claims"]}
        self.assertIn("/article/agent_detail/trajectory/0/observation", paths)
        self.assertIn("/article/agent_detail/recipes/0/adapt", paths)
        self.assertEqual(output["assertions"][0]["omitted_claims"], 0)

    def test_no_source_execution_or_docstring_comment_execution_claim(self):
        text = 'raise RuntimeError("NEVER_EXECUTE")\n"""assert text.endswith("docstring")"""\n# assert text.endswith("comment")\nif False:\n    assert result.endswith("sample")'
        output = assertion_scope({}, [source(text)])
        self.assertEqual(output["recognized_assertions"], 1)
        self.assertIn("conditional or unreachable", output["limit"])
        self.assertIn("custom", output["limit"])
        self.assertNotIn("NEVER_EXECUTE", json.dumps(output))

    def test_negation_boolean_composition_and_bounded_slice_are_not_misclassified(self):
        text = "assert not result.endswith('x')\nassert result.endswith('x') or allowed\nassert result.endswith('x', 2, 4)\nassert result != expected\nassert result.endswith(*suffixes)"
        output = assertion_scope({}, [source(text)])
        self.assertEqual(output["assertions"], [])
        self.assertEqual(output["unsupported_assertions"], 5)

    def test_late_weak_predicate_is_not_crowded_out_by_equality_assertions(self):
        text = "assert result == expected\n" * 90 + "assert result.endswith('sample')"
        output = assertion_scope({}, [source(text)])
        self.assertEqual(output["recognized_assertions"], 91)
        self.assertEqual(output["assertions"][0]["relation"], "suffix predicate")
        self.assertEqual(len(output["assertions"]) + output["omitted_assertions"], 91)

    def test_shell_outputs_and_user_examples_are_not_read_assertion_sources(self):
        events = [source("assert result.endswith('x')", tool="Bash"),
                  {"ref": "E000004", "type": "user.message", "human_input": "assert result.endswith('x')"}]
        self.assertEqual(assertion_scope({}, events)["recognized_assertions"], 0)

    def test_hidden_fields_and_origin_metadata_are_not_indexed(self):
        event = source("assert result.endswith('x')")
        event.update(reasoningText="HIDDEN_CANARY", origin={"path": "PRIVATE_LOCATION"})
        output = json.dumps(assertion_scope({}, [event]))
        self.assertNotIn("HIDDEN_CANARY", output)
        self.assertNotIn("PRIVATE_LOCATION", output)

    def test_read_line_prefixes_are_recorded_not_silently_rewritten(self):
        output = assertion_scope({}, [source(" 1→assert result.endswith('x')\n 2→assert result == expected")])
        self.assertEqual(output["recognized_assertions"], 2)
        self.assertTrue(all(row["normalization"] == "read_line_prefixes_removed" for row in output["assertions"]))

    def test_arrows_in_real_python_string_operands_are_not_display_prefixes(self):
        text = 'assert output == """\n1→alpha\n2→beta\n"""'
        output = assertion_scope({}, [source(text)])
        row = output["assertions"][0]
        self.assertEqual(row["quote"], text)
        self.assertEqual(row["normalization"], "none")

    def test_numbered_multiline_source_preserves_inner_arrows_and_mixed_wrappers_skip(self):
        text = '10→assert output == """\n11→1→alpha\n12→2→beta\n13→"""'
        output = assertion_scope({}, [source(text)])
        self.assertEqual(output["assertions"][0]["quote"], 'assert output == """\n1→alpha\n2→beta\n"""')
        mixed = assertion_scope({}, [source('file header\n1→assert output == expected\n2→assert output.endswith("x")')])
        self.assertEqual(mixed["assertions"], [])
        self.assertEqual(mixed["unparsed_or_large_payloads"], 1)

    def test_only_physical_line_boundaries_are_read_wrappers(self):
        for ending in ("\n", "\r", "\r\n"):
            with self.subTest(ending=ending):
                text = '1→assert output == "one"' + ending + '2→assert output == "two"'
                self.assertEqual(read_display_text(text), 'assert output == "one"' + ending + 'assert output == "two"')

    def test_unicode_separators_inside_operands_survive_indexing_and_grounding(self):
        for separator in ("\u2028", "\u2029", "\x85", "\x0b", "\x0c"):
            with self.subTest(separator=separator):
                statement = 'assert output == "alpha' + separator + '2→beta"'
                text = "10→" + statement + "\r\n11→assert output == expected"
                event = source(text)
                output = assertion_scope({}, [event])
                self.assertEqual(output["assertions"][0]["quote"], statement)
                self.assertIn("2→beta", read_display_text(text))
                self.assertEqual(quote_basis(event, "tool", statement), "literal")
                self.assertIsNone(quote_basis(event, "tool", statement.replace("2→", "")))

    def test_unsupported_or_oversized_payloads_and_budget_omissions_are_explicit(self):
        events = [source("not valid code >>>", "E000001"), source("x" * (MAX_PAYLOAD_CHARS + 1), "E000002")]
        events += [source("assert result.endswith('x')", f"E{index:06d}") for index in range(3, 85)]
        output = assertion_scope({}, events)
        self.assertEqual(output["unparsed_or_large_payloads"], 2)
        self.assertEqual(output["recognized_assertions"], 82)
        self.assertEqual(len(output["assertions"]) + output["omitted_assertions"], 82)
        self.assertLessEqual(len(output["assertions"]), MAX_ASSERTIONS)
        self.assertLessEqual(len(json.dumps(output, ensure_ascii=False)), MAX_INDEX_CHARS)

    def test_index_is_candidate_bound_and_in_actual_prompt_without_new_model_call(self):
        events = packet() + [source("assert result.endswith('sample')")]
        draft = {"article": article(), "brief": brief(), "insights": insights()}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            backend = FakeBackend([edition_review()])
            generate_edition(root, draft, events, backend, validate_article)
            receipt = json.loads((root / "edition-receipt.json").read_bytes())
            index = json.loads((root / "edition-assertions-0.json").read_bytes())
            self.assertEqual(index["candidate_sha256"], receipt["candidate_sha256"])
            self.assertEqual(receipt["identity"]["assertion_scope"], SCHEMA)
            self.assertIn("ASSERTION_SCOPE", backend.prompts[0])
            self.assertEqual(len(backend.calls), 1)
            self.assertEqual(index["recognized_assertions"], 1)
            cached = FakeBackend([])
            generate_edition(root, draft, events, cached, validate_article)
            self.assertFalse(cached.calls)


if __name__ == "__main__":
    unittest.main()
