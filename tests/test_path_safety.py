import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from session_spec.abstract_privacy import plain_path, preflight_tree, write_json
from session_spec.fast_story import plain_path as fast_path
from session_spec.storage import unlinked_path


class PathSafetyTests(unittest.TestCase):
    def test_non_name_surrogate_cloud_metadata_does_not_redirect_paths(self):
        metadata = SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400, st_reparse_tag=0x9000001A)
        with patch.object(Path, "lstat", return_value=metadata):
            self.assertEqual(unlinked_path(Path.cwd()), Path.cwd().resolve())

    def test_unclassified_reparse_point_fails_closed(self):
        metadata = SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400, st_reparse_tag=0)
        with patch.object(Path, "lstat", return_value=metadata), self.assertRaisesRegex(ValueError, "Linked"):
            unlinked_path(Path.cwd())

    @unittest.skipUnless(os.name == "nt", "Windows junction regression")
    def test_junction_leaf_ancestor_and_cycle_refuse_without_touching_outside(self):
        import _winapi

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            owned, outside = root / "owned", root / "outside"
            owned.mkdir()
            outside.mkdir()
            sentinel = outside / "sentinel.json"
            sentinel.write_bytes(b"unchanged")
            junction, cycle = owned / "support", owned / "cycle"
            _winapi.CreateJunction(str(outside), str(junction))
            _winapi.CreateJunction(str(owned), str(cycle))
            try:
                for checker in (plain_path, fast_path):
                    for candidate in (junction, junction / "sentinel.json", junction / "missing" / "child", cycle):
                        with self.subTest(checker=checker.__module__, candidate=str(candidate)):
                            with self.assertRaisesRegex(ValueError, "Linked"):
                                checker(candidate)
                with self.assertRaisesRegex(ValueError, "Linked"):
                    preflight_tree(owned)
                with self.assertRaisesRegex(ValueError, "Linked"):
                    write_json(junction / "sentinel.json", {"modified": True})
                self.assertEqual(sentinel.read_bytes(), b"unchanged")
                self.assertEqual(list(outside.iterdir()), [sentinel])
            finally:
                os.rmdir(junction)
                os.rmdir(cycle)


if __name__ == "__main__":
    unittest.main()
