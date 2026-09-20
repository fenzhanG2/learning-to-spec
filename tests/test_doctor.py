import importlib.util
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location('doctor', Path(__file__).resolve().parents[1] / 'scripts/doctor.py')
DOCTOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DOCTOR)


class DoctorTests(unittest.TestCase):
    def test_node_version_requirement(self):
        for version, expected in [('v16.20.2', False), ('v18.0.0', True), ('v22.12.0', True), ('unknown', False)]:
            with self.subTest(version=version), patch.object(DOCTOR.shutil, 'which', return_value='node'), patch.object(
                    DOCTOR.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, version)):
                self.assertEqual(DOCTOR.supported_node(), expected)

    def test_missing_or_failed_node(self):
        with patch.object(DOCTOR.shutil, 'which', return_value=None):
            self.assertFalse(DOCTOR.supported_node())
        for failure in (OSError('not executable'), subprocess.TimeoutExpired('node', 10)):
            with patch.object(DOCTOR.shutil, 'which', return_value='node'), patch.object(DOCTOR.subprocess, 'run', side_effect=failure):
                self.assertFalse(DOCTOR.supported_node())
        with patch.object(DOCTOR.shutil, 'which', return_value='node'), patch.object(
                DOCTOR.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, 'v22.12.0')):
            self.assertFalse(DOCTOR.supported_node())


if __name__ == '__main__':
    unittest.main()
