import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from contextlib import contextmanager

from .privacy import redact_text, sanitize


@contextmanager
def cleanup_guard(resource, receipt, error_key, directory):
    failed = False
    try:
        yield resource.__enter__()
    except BaseException:
        failed = True
        raise
    finally:
        try:
            resource.__exit__(*sys.exc_info())
        except OSError as error:
            receipt.update({error_key: type(error).__name__, "worker_cleanup_status": "unconfirmed",
                            "worker_directory": str(directory)})
            if not failed:
                raise ValueError("Private generation-worker cleanup was not confirmed; check the local job before retrying.") from None


class ModelResponseError(ValueError):
    def __init__(self, message, response):
        super().__init__(message)
        self.response = redact_text(response)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key: " + key)
        result[key] = value
    return result


def normalize_json_surface(text):
    output = []
    stack = []
    repairs = []
    quoted = False
    escaped = False
    previous = ""
    position = 0
    while position < len(text):
        character = text[position]
        if quoted:
            output.append(character)
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                quoted = False
                previous = '"'
            position += 1
            continue
        if character == '"':
            quoted = True
        elif character in "[{":
            stack.append("]" if character == "[" else "}")
        elif character in "]}":
            if not stack or stack.pop() != character:
                return text, []
        elif character == "," and text[position + 1:].lstrip().startswith(("]", "}")):
            repairs.append("removed trailing comma")
            position += 1
            continue
        elif previous in {"{", ","} and (character.isalpha() or character == "_"):
            match = re.match(r"[A-Za-z_][A-Za-z0-9_]*(?=\s*:)", text[position:])
            if match:
                output.append(json.dumps(match.group()))
                repairs.append("quoted property name")
                position += len(match.group())
                previous = '"'
                continue
        output.append(character)
        if not character.isspace():
            previous = character
        position += 1
    if quoted:
        return text, []
    if stack:
        output.extend(reversed(stack))
        repairs.append("closed balanced containers at end")
    return "".join(output), sorted(set(repairs))


def decode_json(text):
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        value = json.loads(stripped, object_pairs_hook=unique_object)
        repairs = []
    except json.JSONDecodeError as error:
        normalized, repairs = normalize_json_surface(stripped)
        try:
            value = json.loads(normalized, object_pairs_hook=unique_object)
        except ValueError:
            raise ValueError("Copilot did not return a valid single JSON object: " + str(error)) from error
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object, not an array or scalar.")
    return value, repairs


def parse_json(text):
    return decode_json(text)[0]


def find_copilot(override=None):
    if override:
        located = shutil.which(override) or str(Path(override).expanduser())
        if not Path(located).is_file():
            raise ValueError("Copilot executable not found.")
        return located
    if os.name == "nt":
        candidate = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/WinGet/Links/copilot.exe"
        if candidate.is_file():
            return str(candidate)
    located = shutil.which("copilot")
    if not located:
        raise ValueError("Install/authenticate GitHub Copilot CLI before exporting.")
    return located


def auth_environment(gh_host=None):
    environment = os.environ.copy()
    if gh_host:
        result = subprocess.run(
            ["gh", "auth", "token", "--hostname", gh_host],
            capture_output=True, text=True, encoding="utf-8", timeout=30,
        )
        if result.returncode or not result.stdout.strip():
            raise ValueError(f"No gh authentication for {gh_host}; authenticate that host first.")
        environment["COPILOT_GITHUB_TOKEN"] = result.stdout.strip()
        environment["COPILOT_GH_HOST"] = gh_host
        environment["GH_HOST"] = gh_host
        for name in list(environment):
            if name.startswith("COPILOT_PROVIDER_"):
                environment.pop(name, None)
    environment["COPILOT_AUTO_UPDATE"] = "false"
    environment["COPILOT_OTEL_ENABLED"] = "false"
    environment["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"] = "false"
    for name in list(environment):
        if name.startswith("OTEL_") or name in {"COPILOT_CUSTOM_INSTRUCTIONS_DIRS", "COPILOT_ALLOW_ALL"}:
            environment.pop(name, None)
    return environment


def response_from_events(output):
    messages = []
    errors = []
    tool_calls = []
    session_ids = []
    usage = []
    models = set()
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        kind = event.get("type")
        data = event.get("data") or {}
        if isinstance(data.get("model"), str):
            models.add(data["model"])
        if kind == "assistant.message":
            if data.get("content"):
                messages.append(data["content"])
            tool_calls.extend(data.get("toolRequests") or [])
        if kind == "tool.execution_start":
            tool_calls.append({"tool": data.get("toolName")})
        if kind in {"session.error", "error"}:
            errors.append(redact_text(data.get("message") or str(data)))
        if kind == "session.start" and data.get("sessionId"):
            session_ids.append(data["sessionId"])
        if kind == "result":
            if event.get("is_error"):
                errors.append(redact_text(event.get("result", "Copilot reported an error")))
            if event.get("result") and isinstance(event["result"], str):
                messages.append(event["result"])
            usage.append({key: event[key] for key in ("usage", "duration_ms", "cost", "session_id") if key in event})
    if tool_calls:
        names = sorted({str(item.get("tool") or item.get("name") or item.get("toolName") or "unknown") for item in tool_calls})
        raise ValueError("Copilot attempted tool use during historical analysis; export refused: " + ", ".join(names))
    if not messages:
        raise ValueError("Copilot returned no assistant response. " + "; ".join(errors)[-1500:])
    response = messages[-1]
    selection = "last_message"
    if len(messages) > 1:
        for combined in ("".join(messages), "\n".join(messages)):
            try:
                assembled = json.loads(combined, object_pairs_hook=unique_object)
            except ValueError:
                continue
            if isinstance(assembled, dict):
                response = combined
                selection = "joined_complete_json"
                break
    return response, {"tool_calls": 0, "sessions": session_ids, "usage": usage,
                      "models": sorted(models), "message_lengths": [len(message) for message in messages], "response_selection": selection}


def numeric_metrics(value):
    if isinstance(value, dict):
        return {key: numeric_metrics(nested) for key, nested in value.items()
                if isinstance(nested, (dict, int, float)) and not isinstance(nested, bool)}
    return value


def isolated_usage(directory):
    receipts = []
    for filename in sorted((Path(directory) / "session-state").glob("*/events.jsonl")):
        shutdown = None
        try:
            for line in filename.read_text(encoding="utf-8").splitlines():
                event = json.loads(line)
                if event.get("type") == "session.shutdown" and isinstance(event.get("data"), dict):
                    shutdown = event["data"]
        except (OSError, ValueError):
            continue
        if shutdown:
            receipts.append({key: shutdown[key] if key == "currentModel" else numeric_metrics(shutdown[key])
                             for key in ("totalPremiumRequests", "totalNanoAiu", "tokenDetails",
                             "totalApiDurationMs", "modelMetrics", "currentModel") if key in shutdown})
    return receipts


class CopilotBackend:
    def __init__(self, executable=None, model=None, gh_host=None, timeout=600, max_calls=40):
        self.executable = find_copilot(executable)
        self.environment = auth_environment(gh_host)
        self.model = model
        self.timeout = timeout
        self.max_calls = max_calls
        self.calls = []
        self.lock = threading.Lock()

    def generate(self, prompt, label):
        receipt = {"label": label, "input_chars": len(prompt), "provider": "copilot-cli", "requested_model": self.model or "copilot-default"}
        with self.lock:
            if len(self.calls) >= self.max_calls:
                raise ValueError("Model-call budget exhausted; no unreviewed success is reported.")
            self.calls.append(receipt)
        started = time.monotonic()
        worker = tempfile.TemporaryDirectory(prefix="session-spec-worker-")
        with cleanup_guard(worker, receipt, "worker_cleanup_error", worker.name) as directory, \
             cleanup_guard(tempfile.TemporaryFile(dir=directory), receipt, "prompt_cleanup_error", directory) as prompt_input:
            environment = self.environment.copy()
            has_token = any(environment.get(name) for name in ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"))
            if has_token:
                environment["COPILOT_HOME"] = str(Path(directory) / "copilot-home")
            command = [
                self.executable, "--no-auto-update", "--no-custom-instructions",
                "--disable-builtin-mcps", "--available-tools", "__session_spec_no_tools__", "--deny-tool", "shell",
                "--deny-tool", "write", "--deny-tool", "url", "--no-ask-user",
                "--silent", "--stream", "off", "--output-format", "json",
                "--name", "session-spec-" + label, "--log-level", "error",
            ]
            if not has_token:
                config_path = Path(environment.get("COPILOT_HOME", str(Path.home() / ".copilot"))) / "mcp-config.json"
                if config_path.is_file():
                    try:
                        configured = json.loads(config_path.read_text(encoding="utf-8-sig"))
                        for server in configured.get("mcpServers", {}):
                            command.extend(["--disable-mcp-server", server])
                    except (ValueError, OSError):
                        raise ValueError("Cannot safely inspect MCP configuration; use --gh-host for isolated generation.") from None
            if self.model:
                command.extend(["--model", self.model])
            prompt_input.write(prompt.encode("utf-8"))
            prompt_input.seek(0)
            process = subprocess.Popen(
                command, stdin=prompt_input, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=directory, env=environment, text=True, encoding="utf-8", errors="replace",
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            receipt["process_id"] = getattr(process, "pid", None)
            try:
                stdout, stderr = process.communicate(timeout=self.timeout)
            except subprocess.TimeoutExpired:
                receipt["timed_out"] = True
                try:
                    if os.name == "nt":
                        stopped = subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                                 capture_output=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
                        receipt["termination_command_exit_code"] = stopped.returncode
                    else:
                        process.kill()
                    process.communicate(timeout=15)
                except (OSError, subprocess.SubprocessError) as error:
                    receipt["cleanup_error"] = type(error).__name__
                    raise ValueError(f"Copilot timed out during {label}; automatic cleanup was not confirmed. Check the local job before retrying.") from None
                if os.name == "nt" and receipt["termination_command_exit_code"] != 0:
                    receipt["cleanup_error"] = "tree_termination_unconfirmed"
                    raise ValueError(f"Copilot timed out during {label}; process-tree cleanup was not confirmed. Check the local job before retrying.") from None
                raise ValueError(f"Copilot timed out after {self.timeout}s during {label}.") from None
            finally:
                receipt.update({"exit_code": process.returncode, "elapsed_seconds": round(time.monotonic() - started, 2)})
                receipt["session_usage"] = isolated_usage(environment["COPILOT_HOME"]) if has_token else []
                receipt["session_usage_source"] = "isolated-session-shutdown" if receipt["session_usage"] else "unavailable"
            if process.returncode:
                raise ValueError("Copilot failed: " + redact_text(stderr or stdout)[-1800:])
            response, details = response_from_events(stdout)
            receipt.update(details)
            receipt["output_chars"] = len(response)
            try:
                value, repairs = decode_json(response)
                receipt["json_syntax_repairs"] = repairs
                return sanitize(value)
            except ValueError as error:
                receipt["json_error"] = str(error)
                raise ModelResponseError(str(error), response) from error
