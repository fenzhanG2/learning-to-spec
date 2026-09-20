import contextlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from session_spec.cli import parser
from session_spec.delivery import deliver, filenames, preferences, validate_delivery
from session_spec.private_cli import generate_private
from session_spec.reduction import review_identity


class DeliveryTests(unittest.TestCase):
    def test_six_explicit_reader_destination_combinations_deliver_only_selected_files(self):
        for readers in ("human", "agent", "both"):
            for destination in ("local", "artifactstore"):
                with self.subTest(readers=readers, destination=destination), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    audience = "local" if destination == "local" else "root"
                    selection = preferences(readers, destination, audience)
                    (root / "reduced").mkdir()
                    (root / "reduced/reduction.json").write_text(json.dumps({"preferences": selection, "audience": audience, "review_id": "test"}))
                    (root / "story").mkdir()
                    for name in filenames("both"):
                        (root / "story" / name).write_text("Selected fixture: " + name)
                    (root / "story/raw-session.json").write_text("Must not travel")
                    result = deliver(root, selection)
                    self.assertEqual(set(result["files"]), set(filenames(readers)))
                    with zipfile.ZipFile(root / "deliverables.zip") as archive:
                        self.assertEqual(set(archive.namelist()), set(filenames(readers)))
                    self.assertEqual(validate_delivery(root, selection)["preferences"], selection)
                    first = filenames(readers)[0]
                    (root / "deliverables" / first).write_text("Changed")
                    with self.assertRaises(ValueError):
                        validate_delivery(root, selection)

    def test_reader_and_destination_are_hash_bound_and_cannot_be_inferred(self):
        for readers, destination, audience in ((None, "local", "local"), ("both", None, "local"), ("human", "local", "root"), ("agent", "artifactstore", "local")):
            with self.assertRaises(ValueError):
                preferences(readers, destination, audience)
        source = {"schema": "privacy-review/v1", "source_sha256": "source", "baseline_sha256": "base", "purpose": "test", "audience": "local", "findings": [], "semantic": {}}
        self.assertNotEqual(review_identity({**source, "preferences": {"readers": "human", "destination": "local"}}),
                            review_identity({**source, "preferences": {"readers": "agent", "destination": "local"}}))

    def test_cli_has_no_default_reader_or_delivery_and_generation_needs_confirmation(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parser().parse_args(["privacy-scan", "session", "--out", "review"])
        with patch("session_spec.private_cli.run_story") as backend:
            with self.assertRaisesRegex(ValueError, "confirmation"):
                generate_private("review", {}, "output", "home")
            backend.assert_not_called()
