import hashlib
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from contextlib import contextmanager
from contextvars import ContextVar

from .privacy import redact_text, sanitize


_PREPARATION = ContextVar("session_spec_preparation", default=None)


class PreparationTimeoutError(TimeoutError):
    def __init__(self, cleanup_confirmed=True):
        self.cleanup_confirmed = cleanup_confirmed
        super().__init__("The configured preparation time limit was reached." if cleanup_confirmed else
                         "The configured time limit was reached; cleanup is unconfirmed. Do not retry until the local job is checked.")

    @property
    def error_code(self):
        return "timeout" if self.cleanup_confirmed else "cleanup_unconfirmed"


class PreparationCallLimitError(RuntimeError):
    error_code = "call_budget"

    def __init__(self):
        super().__init__("The shared preparation model-call limit was reached; no further model call was started.")


class CopilotCapabilityError(RuntimeError):
    def __init__(self, message, receipt):
        super().__init__(message)
        self.receipt = receipt


def bounded_seconds(value, maximum, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value <= maximum:
        raise ValueError(f"{name} must be positive finite seconds, at most {maximum}.")
    return float(value)


class PreparationBudget:
    def __init__(self, total_seconds=120, call_seconds=60, max_calls=3):
        self.total_seconds = bounded_seconds(total_seconds, 300, "preparation_timeout")
        self.call_seconds = bounded_seconds(call_seconds, 120, "model_timeout")
        if isinstance(max_calls, bool) or not isinstance(max_calls, int) or not 0 <= max_calls <= 3:
            raise ValueError("Preparation max_calls must be an integer from 0 to 3.")
        self.max_calls = max_calls
        self.started = time.monotonic()
        self.deadline = self.started + self.total_seconds
        self.cleanup_reserve = min(5.0, self.total_seconds / 10)
        self.model_calls = 0
        self.reserved_calls = 0
        self.calls = []
        self.local_processes = []
        self.failure = None
        self.lock = threading.RLock()

    def check(self):
        with self.lock:
            if self.failure is not None:
                raise self.failure
            if time.monotonic() >= self.deadline:
                self.failure = PreparationTimeoutError()
                raise self.failure

    def call_timeout(self, timeout):
        self.check()
        remaining = self.deadline - time.monotonic() - self.cleanup_reserve
        if remaining <= 0:
            self.fail(PreparationTimeoutError())
        return min(timeout, self.call_seconds, remaining)

    def reserve(self, receipt):
        with self.lock:
            self.check()
            if self.model_calls + self.reserved_calls >= self.max_calls:
                self.fail(PreparationCallLimitError())
            self.reserved_calls += 1
            receipt["provider_started"] = False

    def launched(self, receipt):
        with self.lock:
            self.reserved_calls -= 1
            self.model_calls += 1
            receipt["provider_started"] = True
            receipt["shared_call_number"] = self.model_calls

    def release(self):
        with self.lock:
            self.reserved_calls -= 1

    def fail(self, error):
        with self.lock:
            self.failure = error
        raise error

    def receipt(self):
        return {"total_seconds": self.total_seconds, "model_timeout": self.call_seconds, "max_calls": self.max_calls,
                "model_calls": self.model_calls, "elapsed_seconds": round(time.monotonic() - self.started, 3),
                "cleanup_reserve_seconds": self.cleanup_reserve, "calls": self.calls, "local_processes": self.local_processes}


@contextmanager
def preparation_budget(total_seconds=120, call_seconds=60, max_calls=3):
    existing = _PREPARATION.get()
    budget = existing or PreparationBudget(total_seconds, call_seconds, max_calls)
    token = _PREPARATION.set(budget)
    try:
        budget.check()
        yield budget
        budget.check()
    finally:
        _PREPARATION.reset(token)


def check_preparation_deadline():
    budget = _PREPARATION.get()
    if budget is not None:
        budget.check()


def stop_process_tree(process, receipt, budget=None):
    cleanup_deadline = time.monotonic() + 30 if budget is None else min(budget.deadline, time.monotonic() + 5)

    def cleanup_timeout():
        return max(0.001, min(15, cleanup_deadline - time.monotonic()))

    try:
        if os.name == "nt":
            stopped = subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                     capture_output=True, timeout=cleanup_timeout(), creationflags=subprocess.CREATE_NO_WINDOW)
            receipt["termination_command_exit_code"] = stopped.returncode
        else:
            os.killpg(process.pid, signal.SIGKILL)
        process.communicate(timeout=cleanup_timeout())
    except (OSError, subprocess.SubprocessError) as error:
        receipt["cleanup_error"] = type(error).__name__
        receipt["process_cleanup_status"] = "unconfirmed"
        return False
    if process.returncode is None or (os.name == "nt" and receipt["termination_command_exit_code"] != 0):
        receipt["cleanup_error"] = "tree_termination_unconfirmed"
        receipt["process_cleanup_status"] = "unconfirmed"
        return False
    receipt["process_cleanup_status"] = "confirmed"
    return True


def run_preparation_process(command, *, timeout=None, capture_output=False, check=False, budget=None, **options):
    budget = _PREPARATION.get() or budget
    if budget is None:
        return subprocess.run(command, timeout=timeout, capture_output=capture_output, check=check, **options)
    if capture_output:
        if "stdout" in options or "stderr" in options:
            raise ValueError("capture_output cannot be combined with explicit output streams")
        options.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    options.update(start_new_session=os.name != "nt", creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    receipt = {"kind": "local_process", "model_call": False}
    budget.local_processes.append(receipt)
    budget.call_timeout(timeout if timeout is not None else budget.total_seconds)
    started = time.monotonic()
    process = subprocess.Popen(command, **options)
    receipt["process_id"] = process.pid
    try:
        bounded = budget.call_timeout(timeout if timeout is not None else budget.total_seconds)
        receipt["timeout_seconds"] = bounded
        stdout, stderr = process.communicate(timeout=bounded)
        budget.check()
    except (subprocess.TimeoutExpired, PreparationTimeoutError):
        receipt["timed_out"] = True
        budget.fail(PreparationTimeoutError(stop_process_tree(process, receipt, budget)))
    finally:
        receipt.update(exit_code=process.returncode, elapsed_seconds=round(time.monotonic() - started, 3))
    if check and process.returncode:
        raise subprocess.CalledProcessError(process.returncode, command, output=stdout, stderr=stderr)
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


@contextmanager
def cleanup_guard(resource, receipt, error_key, directory):
    failed = False
    failure = None
    try:
        yield resource.__enter__()
    except BaseException as error:
        failed = True
        failure = error
        raise
    finally:
        try:
            resource.__exit__(*sys.exc_info())
        except OSError as error:
            receipt.update({error_key: type(error).__name__, "worker_cleanup_status": "unconfirmed",
                            "worker_directory": str(directory)})
            if isinstance(failure, PreparationTimeoutError):
                failure.cleanup_confirmed = False
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


def declared_cli_options(output):
    declarations = []
    pattern = r"^([ \t]*)(-(?:-[a-z][a-z0-9-]*|[A-Za-z0-9])(?:[ \t]*,[ \t]*-(?:-[a-z][a-z0-9-]*|[A-Za-z0-9]))*)(?=[ \t=<\[]|$)"
    for line in output.splitlines():
        match = re.match(pattern, line.expandtabs())
        if match:
            declarations.append((len(match[1]), match[2]))
    indentation = min((indent for indent, declaration in declarations), default=0)
    columns = {indentation}
    for indent, declaration in declarations:
        if indent == indentation:
            aliases = re.match(r"(?:-[A-Za-z0-9][ \t]*,[ \t]*)+(?=--)", declaration)
            if aliases:
                columns.add(indent + len(aliases[0]))
    return {flag for indent, declaration in declarations if indent in columns
            for flag in re.findall(r"--[a-z][a-z0-9-]*", declaration)}


def probe_copilot_options(executable, environment, directory, requested, receipt, *, budget=None):
    started = time.monotonic()
    capability = {"status": "checking", "source": "worker-environment-help", "requested_flags": sorted(requested),
                  "executable": str(executable), "version": None, "exit_codes": {}}
    receipt["cli_capabilities"] = capability
    try:
        for argument in ("--version", "--help"):
            result = run_preparation_process([executable, argument], cwd=directory, env=environment,
                                             stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                             encoding="utf-8", errors="replace", timeout=5, budget=budget)
            capability["exit_codes"][argument] = result.returncode
            if result.returncode:
                raise CopilotCapabilityError("Copilot CLI capability check failed; no session text was dispatched.", receipt)
            output = (result.stdout or "") + (result.stderr or "")
            if len(output.encode("utf-8")) > 256 * 1024:
                raise CopilotCapabilityError("Copilot CLI capability output exceeded its bound; no session text was dispatched.", receipt)
            if argument == "--version":
                match = re.search(r"(?im)^GitHub Copilot CLI\s+([0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?)\.?\s*$", output)
                capability["version"] = match[1].rstrip(".") if match else None
            else:
                capability["help_sha256"] = hashlib.sha256(output.encode("utf-8")).hexdigest()
                advertised = declared_cli_options(output)
                capability["supported_requested_flags"] = sorted(requested & advertised)
                capability["unsupported_flags"] = sorted(requested - advertised)
        if capability["unsupported_flags"]:
            capability["status"] = "unsupported"
            raise CopilotCapabilityError("The resolved Copilot CLI in the isolated worker environment does not advertise "
                                         + ", ".join(capability["unsupported_flags"])
                                         + "; no session text was dispatched and no model/tier fallback was applied.", receipt)
        capability["status"] = "supported"
    except (PreparationTimeoutError, CopilotCapabilityError, OSError, subprocess.SubprocessError) as error:
        if capability["status"] == "checking":
            capability["status"] = "failed"
        capability["failure_type"] = type(error).__name__
        if isinstance(error, (PreparationTimeoutError, CopilotCapabilityError)):
            raise
        raise CopilotCapabilityError("Copilot CLI capability check could not complete; no session text was dispatched.", receipt) from None
    finally:
        capability["elapsed_seconds"] = round(time.monotonic() - started, 3)


def auth_environment(gh_host=None):
    environment = os.environ.copy()
    if gh_host:
        budget = _PREPARATION.get()
        try:
            result = subprocess.run(
                ["gh", "auth", "token", "--hostname", gh_host],
                capture_output=True, text=True, encoding="utf-8",
                timeout=budget.call_timeout(30) if budget is not None else 30,
            )
        except subprocess.TimeoutExpired:
            if budget is not None:
                budget.fail(PreparationTimeoutError(False))
            raise
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
    def __init__(self, executable=None, model=None, gh_host=None, timeout=600, max_calls=40, reasoning_effort=None, auto_tier=None):
        if reasoning_effort is not None and (not isinstance(reasoning_effort, str) or
                reasoning_effort not in {"none", "minimal", "low", "medium", "high", "xhigh", "max"}):
            raise ValueError("Unsupported requested reasoning effort.")
        if model == "auto" and reasoning_effort is not None:
            raise ValueError("The auto model does not support reasoning effort configuration; omit reasoning_effort.")
        self.reasoning_effort = reasoning_effort
        if auto_tier is not None and (not isinstance(auto_tier, str) or auto_tier not in {"fast", "efficiency", "balance", "intelligence"}):
            raise ValueError("Unsupported requested auto tier.")
        if auto_tier is not None and model != "auto":
            raise ValueError("An auto tier requires an explicit auto model selection.")
        self.auto_tier = auto_tier
        self.preparation = _PREPARATION.get()
        check_preparation_deadline()
        self.executable = find_copilot(executable)
        self.environment = auth_environment(gh_host)
        check_preparation_deadline()
        self.model = model
        self.timeout = timeout
        self.max_calls = max_calls
        self.calls = []
        self.lock = threading.Lock()

    def generate(self, prompt, label):
        receipt = {"label": label, "input_chars": len(prompt), "provider": "copilot-cli", "requested_model": self.model or "copilot-default",
                   "requested_reasoning_effort": self.reasoning_effort, "requested_auto_tier": self.auto_tier,
                   "provider_started": False, "effective_model": None, "effective_auto_tier": None,
                   "effective_reasoning_effort": None, "effective_settings_status": "not_dispatched"}
        budget = _PREPARATION.get() or self.preparation
        if budget is not None:
            with budget.lock:
                budget.calls.append(receipt)
            budget.check()
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
            requested = ({"--auto-tier"} if self.auto_tier is not None else set()) | ({"--reasoning-effort"} if self.reasoning_effort is not None else set())
            if requested:
                probe_copilot_options(self.executable, environment, directory, requested, receipt, budget=budget)
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
            if self.auto_tier is not None:
                command.extend(["--auto-tier", self.auto_tier])
            if self.reasoning_effort is not None:
                command.extend(["--reasoning-effort", self.reasoning_effort])
            prompt_input.write(prompt.encode("utf-8"))
            prompt_input.seek(0)
            if budget is not None:
                budget.call_timeout(self.timeout)
                budget.reserve(receipt)
            try:
                process = subprocess.Popen(
                    command, stdin=prompt_input, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    cwd=directory, env=environment, text=True, encoding="utf-8", errors="replace",
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                    start_new_session=os.name != "nt",
                )
            except BaseException:
                if budget is not None:
                    budget.release()
                raise
            if budget is not None:
                budget.launched(receipt)
            receipt.update(provider_started=True, effective_settings_status="dispatched_unconfirmed")
            receipt["process_id"] = getattr(process, "pid", None)
            try:
                timeout = budget.call_timeout(self.timeout) if budget is not None else self.timeout
                receipt["timeout_seconds"] = timeout
                stdout, stderr = process.communicate(timeout=timeout)
                if budget is not None:
                    budget.check()
            except (subprocess.TimeoutExpired, PreparationTimeoutError):
                receipt["timed_out"] = True
                confirmed = stop_process_tree(process, receipt, budget)
                if budget is not None:
                    budget.fail(PreparationTimeoutError(confirmed))
                if not confirmed:
                    raise ValueError(f"Copilot timed out during {label}; process-tree cleanup was not confirmed. Check the local job before retrying.") from None
                raise ValueError(f"Copilot timed out after {self.timeout}s during {label}.") from None
            finally:
                receipt.update({"exit_code": process.returncode, "elapsed_seconds": round(time.monotonic() - started, 2)})
                receipt["session_usage"] = isolated_usage(environment["COPILOT_HOME"]) if has_token else []
                receipt["session_usage_source"] = "isolated-session-shutdown" if receipt["session_usage"] else "unavailable"
            if process.returncode:
                raise ValueError("Copilot failed: " + redact_text(stderr or stdout)[-1800:])
            receipt.update(effective_model=self.model or "copilot-default", effective_auto_tier=self.auto_tier,
                           effective_reasoning_effort=self.reasoning_effort, effective_settings_status="cli_accepted")
            response, details = response_from_events(stdout)
            receipt.update(details)
            receipt["output_chars"] = len(response)
            try:
                value, repairs = decode_json(response)
                receipt["json_syntax_repairs"] = repairs
                value = sanitize(value)
                if budget is not None:
                    budget.check()
                return value
            except ValueError as error:
                receipt["json_error"] = str(error)
                raise ModelResponseError(str(error), response) from error
