import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from session_spec.artifacts import ArtifactAuthError, ArtifactError, ArtifactExistsError, ArtifactNameError
from session_spec.native_bridge import native_error


class NativeErrorTests(unittest.TestCase):
    def test_errors_are_typed_and_diagnostics_stay_local(self):
        cases = [
            ("plan", ArtifactNameError("PRIVATE_CANARY"), "artifact_name_invalid"),
            ("plan", ArtifactExistsError("PRIVATE_CANARY"), "artifact_exists"),
            ("plan", ArtifactAuthError("PRIVATE_CANARY"), "artifact_auth_required"),
            ("plan", ArtifactError(403), "artifact_auth_required"),
            ("plan", OSError("PRIVATE_CANARY"), "artifact_plan_failed"),
            ("publish", OSError("PRIVATE_CANARY"), "publication_unconfirmed"),
            ("capture", ValueError("PRIVATE_CANARY"), "operation_failed"),
            ([], TypeError("PRIVATE_CANARY"), "operation_failed"),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            bridge = SimpleNamespace(root=Path(temporary))
            for operation, error, code in cases:
                response = native_error(bridge, operation, error)
                self.assertEqual(response["error_code"], code)
                self.assertEqual(set(response), {"error", "error_code"})
                self.assertNotIn("PRIVATE_CANARY", json.dumps(response))
            diagnostics = list((bridge.root / "diagnostics").glob("*.json"))
            self.assertEqual(len(diagnostics), len(cases))
            entries = [json.loads(path.read_bytes()) for path in diagnostics]
            self.assertTrue(any(entry["message"] == "PRIVATE_CANARY" for entry in entries))
            self.assertTrue(any(entry["operation"] == "unknown" for entry in entries))
            self.assertTrue(all(set(entry) == {"operation", "code", "error_type", "message"} for entry in entries))

    def test_diagnostic_write_failure_does_not_break_error_protocol(self):
        with tempfile.TemporaryDirectory() as temporary, patch("session_spec.native_bridge.write_json", side_effect=OSError("PRIVATE_DISK_ERROR")):
            response = native_error(SimpleNamespace(root=Path(temporary)), "plan", ArtifactNameError("PRIVATE_NAME"))
        self.assertEqual(response["error_code"], "artifact_name_invalid")
        self.assertNotIn("PRIVATE_", json.dumps(response))
