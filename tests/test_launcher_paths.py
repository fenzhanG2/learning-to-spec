import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from session_spec.runtime import LocalClient, VERSION


ROOT = Path(__file__).resolve().parents[1]


class LauncherPathTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node launcher boundary regressions")
    def test_launcher_encoding_and_directory_fallback_boundaries(self):
        environment = {**os.environ, "TEST_PYTHON_EXECUTABLE": sys.executable}
        result = subprocess.run([shutil.which("node"), "--test", str(ROOT / "tests/launcher_paths.cjs")],
                                cwd=ROOT, env=environment, capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_runtime_uses_private_working_directory_and_absolute_profile(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            client = LocalClient(root / "runtime")
            process = Mock()
            with patch.dict(os.environ, {"COPILOT_HOME": "relative-fixture-home"}), \
                    patch.object(client, "request", side_effect=[OSError("not started"), {"version": VERSION}]), \
                    patch("session_spec.runtime.subprocess.Popen", return_value=process) as launch, \
                    patch("session_spec.runtime.time.sleep"):
                client.ensure()
            self.assertEqual(launch.call_args.kwargs["cwd"], client.root)
            command = launch.call_args.args[0]
            self.assertEqual(command[0], sys.executable)
            self.assertEqual(command[command.index("--home") + 1], str(Path("relative-fixture-home").resolve()))

    @unittest.skipUnless(shutil.which("node"), "Node launcher integration")
    def test_initialized_launcher_and_runtime_release_disposable_plugin_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            plugin = root / "plugin"
            (plugin / "scripts").mkdir(parents=True)
            for name in ("plugin_mcp.cjs", "plugin_mcp.py", "plugin_runtime.py"):
                shutil.copy2(ROOT / "scripts" / name, plugin / "scripts" / name)
            shutil.copy2(ROOT / "plugin.json", plugin / "plugin.json")
            shutil.copytree(ROOT / "session_spec", plugin / "session_spec", ignore=shutil.ignore_patterns("__pycache__", "web"))
            environment = os.environ.copy()
            environment.update(COPILOT_HOME="../empty-fixture-home", LEARNING_TO_SPEC_HOME="../runtime",
                               PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-16")
            responses = queue.Queue()
            with (root / "stderr.log").open("w", encoding="utf-8") as errors:
                process = subprocess.Popen([shutil.which("node"), str(plugin / "scripts/plugin_mcp.cjs")],
                                           cwd=plugin, env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                           stderr=errors, text=True, encoding="utf-8",
                                           creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                reader = threading.Thread(target=lambda: [responses.put(line) for line in process.stdout], daemon=True)
                reader.start()

                def request(identifier, method, params=None):
                    process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": identifier, "method": method,
                                                    "params": params or {}}) + "\n")
                    process.stdin.flush()
                    response = json.loads(responses.get(timeout=30))
                    self.assertEqual(response["id"], identifier)
                    self.assertNotIn("error", response)
                    self.assertFalse(response["result"].get("isError"), response)
                    return response

                try:
                    request(1, "initialize")
                    request(2, "tools/call", {"name": "health", "arguments": {}})
                    self.assertTrue((root / "runtime/endpoint.json").is_file())
                    self.assertFalse((plugin / "runtime").exists())
                    resolved = plugin.resolve(strict=True)
                    self.assertEqual(resolved.parent, root)
                    self.assertFalse(plugin.is_symlink())
                    shutil.rmtree(resolved)
                    self.assertFalse(plugin.exists())
                    self.assertIsNone(process.poll())
                    request(3, "tools/call", {"name": "shutdown", "arguments": {}})
                finally:
                    try:
                        LocalClient(root / "runtime").request("shutdown", {})
                    except (OSError, ValueError):
                        pass
                    process.stdin.close()
                    try:
                        process.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        if os.name == "nt":
                            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True)
                        else:
                            process.kill()
                        process.wait(timeout=10)
                    reader.join(timeout=5)
                    process.stdout.close()
                    for attempt in range(100):
                        if not (root / "runtime/endpoint.json").exists():
                            break
                        time.sleep(0.05)
                self.assertEqual(process.returncode, 0, (root / "stderr.log").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
