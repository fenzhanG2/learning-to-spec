import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from session_spec.ingest import metadata_rows, read_session
from session_spec.privacy import sanitize
from session_spec.runtime import DurableStudio
from session_spec.studio import Studio


class SessionDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.source = self.root / "selected/events.jsonl"
        self.source.parent.mkdir()
        self.source.write_text(json.dumps({"type": "user.message", "data": {"content": "Synthetic change request"}}) + "\n", encoding="utf-8")
        with closing(sqlite3.connect(self.home / "data.db")) as connection:
            connection.execute("CREATE TABLE sessions (id TEXT, title TEXT)")
            connection.executemany("INSERT INTO sessions VALUES (?, ?)",
                                   [("selected", "Selected synthetic session"), ("unrelated", "UNRELATED_TITLE_CANARY")])
            connection.commit()

    def tearDown(self):
        self.temporary.cleanup()

    def test_health_and_exact_open_never_discover_other_sessions(self):
        with patch("session_spec.studio.list_sessions", side_effect=AssertionError("Unrequested discovery")), \
                patch("session_spec.runtime.list_sessions", side_effect=AssertionError("Unrequested discovery")), \
                patch("session_spec.runtime.open_local"):
            studio = DurableStudio(self.home, self.root / "jobs")
            self.assertEqual(studio.sessions, [])
            self.assertEqual(studio.host_action("health", {}, "http://127.0.0.1:1234")["running"], 0)
            studio.host_action("open_studio", {"session": str(self.source)}, "http://127.0.0.1:1234")
            self.assertEqual([item["path"] for item in studio.sessions], [str(self.source.resolve())])
            studio.jobs["a" * 32] = {"id": "a" * 32}
            studio.host_action("open_studio", {"job": "a" * 32}, "http://127.0.0.1:1234")

    def test_empty_studio_explicitly_discovers_metadata_for_picker(self):
        studio = DurableStudio(self.home, self.root / "jobs")
        with patch("session_spec.runtime.list_sessions", return_value=[{"id": "synthetic"}]) as listing, \
                patch("session_spec.runtime.open_local"):
            studio.host_action("open_studio", {}, "http://127.0.0.1:1234")
            listing.assert_called_once_with(self.home.resolve(), limit=100)
            self.assertEqual(studio.sessions, [{"id": "synthetic"}])

    def test_manual_exact_source_does_not_discover_sessions(self):
        with patch("session_spec.studio.list_sessions", side_effect=AssertionError("Unrequested discovery")):
            studio = Studio(self.home, self.root / "manual", session=str(self.source))
            self.assertEqual(len(studio.sessions), 1)

    def test_exact_read_only_materializes_its_matching_metadata_row(self):
        def only_selected(value):
            self.assertNotEqual(value.get("id"), "unrelated")
            return sanitize(value)

        with patch("session_spec.ingest.sanitize", side_effect=only_selected):
            metadata, records = read_session(self.source, self.home)
        self.assertEqual(metadata["title"], "Selected synthetic session")
        self.assertEqual(len(records), 1)

    def test_exact_metadata_query_is_parameterized_and_discovery_remains_available(self):
        self.assertEqual(metadata_rows(self.home, "selected' OR 1=1 --"), {})
        self.assertEqual(set(metadata_rows(self.home, "missing")), set())
        self.assertEqual(set(metadata_rows(self.home)), {"selected", "unrelated"})


if __name__ == "__main__":
    unittest.main()
