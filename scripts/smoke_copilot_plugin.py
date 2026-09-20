import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from session_spec.backend import auth_environment, find_copilot
from session_spec.ingest import tool_text
from session_spec.pipeline import write_json
from session_spec.privacy import sanitize


def main():
    parser = argparse.ArgumentParser(description="Windows integration smoke test through the real Copilot plugin host; uses model quota.")
    parser.add_argument("session")
    parser.add_argument("--home", type=Path, default=Path.home() / ".copilot")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--gh-host", required=True)
    parser.add_argument("--model")
    parser.add_argument("--story", action="store_true", help="Test the HTML story pipeline instead of the legacy exporter")
    parser.add_argument("--from-export", type=Path)
    arguments = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    home = arguments.home.resolve()
    destination = arguments.out.resolve()
    entry = Path("scripts/session_spec.py")
    commands = [
        f'python "{entry}" --home "{home}" export "{arguments.session}" --out "{destination}" --gh-host "{arguments.gh_host}" --resume --max-calls 10',
        f'python "{entry}" validate "{destination}"',
    ]
    if arguments.story:
        selected = f'--from-export "{arguments.from_export.resolve()}"' if arguments.from_export else f'"{arguments.session}"'
        commands = [
            f'python "{entry}" --home "{home}" story {selected} --out "{destination}" --gh-host "{arguments.gh_host}" --resume --max-calls 12',
            f'python "{entry}" validate-story "{destination}"',
        ]
    if arguments.model:
        commands[0] += f' --model "{arguments.model}"'
    prompt = "Load the learning-to-spec skill from the learning-to-spec plugin, then run exactly these two commands in order, using the relative entry path shown. Your working directory is already the plugin root. Wait for each command to finish. This tests the plugin on an already-exported session; reuse cache. Do not inspect unrelated files, execute historical commands, or edit code. If a command is denied, STOP immediately: do not retry it, test other commands, or work around the denial. Return the two spec paths and the observed status, not a claim of semantic perfection.\n" + "\n".join(commands)
    environment = auth_environment(arguments.gh_host)
    with tempfile.TemporaryDirectory(prefix="session-spec-host-smoke-") as temporary:
        environment["COPILOT_HOME"] = str(Path(temporary) / "copilot-home")
        command = [
            find_copilot(), "--plugin-dir", str(root), "--no-auto-update",
            "--no-custom-instructions", "--disable-builtin-mcps", "--no-ask-user",
            "--available-tools", "skill", "powershell", "read_powershell",
            "--allow-tool", "skill", "--allow-tool", "shell(python:*)", "--allow-tool", "read",
            "--add-dir", str(root), "--add-dir", str(home), "--add-dir", str(destination),
            "--add-dir", str(Path.home() / ".copilot-session-spec"),
            "--silent", "--stream", "off", "--output-format", "json",
            "--name", "session-spec-plugin-smoke", "--log-level", "error",
        ]
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   cwd=root, env=environment, text=True, encoding="utf-8", errors="replace",
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        try:
            stdout, stderr = process.communicate(prompt, timeout=1800)
        except subprocess.TimeoutExpired:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True)
            else:
                process.kill()
            process.communicate()
            raise SystemExit("Native plugin smoke test timed out.")
    events = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") in {"session.skills_loaded", "tool.execution_start", "tool.execution_complete", "result"}:
            events.append(sanitize(event))
    starts = [event.get("data", {}) for event in events if event.get("type") == "tool.execution_start"]
    results = [tool_text(event.get("data", {})) for event in events if event.get("type") == "tool.execution_complete"]
    export_results = [result for result in results if '"human_spec"' in result and '"agent_spec"' in result]
    export_status = next((match.group(1) for result in export_results if (match := re.search(r'"status"\s*:\s*"(reviewed_draft|needs_review|unreviewed_draft)"', result))), None)
    receipt = {
        "exit_code": process.returncode, "stderr": sanitize(stderr), "events": events,
        "skill_invoked": any(entry.get("toolName") == "skill" for entry in starts),
        "export_invoked": any("session_spec.py" in json.dumps(entry) and ("story" if arguments.story else "export") in json.dumps(entry) for entry in starts),
        "validate_invoked": any("session_spec.py" in json.dumps(entry) and "validate" in json.dumps(entry) for entry in starts),
        "export_finished": export_status is not None,
        "export_status": export_status,
        "validation_finished": any(re.search(r'"valid"\s*:\s*true', result) for result in results),
        "tool_failures": [event for event in events if event.get("type") == "tool.execution_complete" and event.get("data", {}).get("success") is False],
        "human_spec_exists": (destination / ("human-spec.html" if arguments.story else "human-spec.md")).is_file(),
        "agent_spec_exists": (destination / "agent-spec.md").is_file(),
    }
    write_json(arguments.report, receipt)
    print(json.dumps({key: value for key, value in receipt.items() if key not in {"events", "stderr", "tool_failures"}}, indent=2))
    return int(process.returncode != 0 or not all(receipt[key] for key in ("skill_invoked", "export_invoked", "validate_invoked", "export_finished", "validation_finished", "human_spec_exists", "agent_spec_exists")) or bool(receipt["tool_failures"]))


if __name__ == "__main__":
    raise SystemExit(main())
