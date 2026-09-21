import argparse
import copy
import ctypes
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
from contextlib import contextmanager
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .abstract_privacy import PIPELINE, load_abstract_review, prepare_abstract_review
from .backend import PreparationBudget, PreparationCallLimitError, PreparationTimeoutError, bounded_seconds, preparation_budget
from .delivery import preferences
from .ingest import origin_of
from .privacy_presentation import present_review
from .privacy import HIDDEN_FIELDS, SECRET_FIELD, content_redaction, redaction_enabled, sanitize
from .reduction import LIMIT, load_review
from .runtime import DurableStudio, FileLease, runtime_root
from .storage import write_json


WEB = Path(__file__).parent / "web"


def windows_markdown_handler():
    query = ctypes.WinDLL("shlwapi").AssocQueryStringW
    query.argtypes = [ctypes.c_uint, ctypes.c_uint, ctypes.c_wchar_p, ctypes.c_wchar_p,
                      ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_uint)]
    query.restype = ctypes.c_long
    size = ctypes.c_uint(32768)
    result = ctypes.create_unicode_buffer(size.value)
    return (query(0, 2, ".md", "open", result, ctypes.byref(size)) == 0
            and bool(result.value) and Path(result.value).name.lower() != "openwith.exe")


def open_local_output(target):
    if sys.platform == "win32":
        if target.suffix == ".md" and not windows_markdown_handler():
            subprocess.Popen([str(Path(os.environ["WINDIR"]) / "System32/notepad.exe"), str(target)], shell=False)
            return
        try:
            os.startfile(str(target), "open")
        except OSError as error:
            if getattr(error, "winerror", None) != 1155 or target.suffix != ".md":
                raise
            subprocess.Popen([str(Path(os.environ["WINDIR"]) / "System32/notepad.exe"), str(target)], shell=False)
    else:
        subprocess.run(["open" if sys.platform == "darwin" else "xdg-open", str(target)],
                       shell=False, check=True, timeout=5, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


EVENT_FIELDS = {
    "session.start": {"sessionId"},
    "user.message": {"content", "source", "agentId", "parentToolCallId", "sourceTurnId"},
    "assistant.message": {"content", "source", "agentId", "parentToolCallId", "messageId", "sourceTurnId"},
    "tool.execution_start": {"toolCallId", "toolName", "arguments", "agentId", "parentToolCallId"},
    "tool.execution_complete": {"toolCallId", "toolName", "result", "success", "error", "agentId", "parentToolCallId"},
    "session.imported_context": {"content", "sourceTurnId", "sourceContext", "importMetadata"},
    "session.task_complete": {"summary", "success", "error"},
}
PAYLOAD_FIELDS = {
    "content", "text", "stdout", "stderr", "exitCode", "exit_code", "returncode", "success", "error", "message", "code", "type",
    "command", "path", "file_path", "filename", "cwd", "pattern", "query", "old_string", "new_string", "replace_all",
    "start_line", "end_line", "offset", "limit", "timeout", "description", "summary", "result", "arguments",
    "source", "format", "version", "origin", "role", "kind", "sourceSessionId", "sourceTurnId",
}
VISIBLE_SOURCES = {"user", "human", "assistant", "root", "swe-chat"}
ATTRIBUTION_FIELDS = {"agentId", "parentToolCallId", "parentAgentTaskId", "source"}
VISIBLE_PAYLOAD_TYPES = {"text", "code", "markdown", "output", "success", "failure", "error"}
NATIVE_URL = re.compile(r"(?:https?|wss?):(?:\\?/){2}[^\s<>\"'`]+", re.I)
PRIVATE_TOOL = re.compile(r"canvas|elicit|request_user_input", re.I)
CONTROL_TEXT = re.compile(r"^\s*<(?:canvas-context|elicitation|system|control)(?:[\s>/-])", re.I)
OMITTED_PAYLOAD = object()


def session_uuid(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}", value):
        raise ValueError("A host-provided session UUID is required")
    return str(uuid.UUID(value))


def observable_text(value):
    def replace_url(match):
        candidate = urllib.parse.unquote(match[0].replace("\\/", "/"))
        if re.search(r"canvas|(?:[?#&;/]|^)(?:access|token|auth|authorization|key|secret|sig|capability|bearer|code)=", candidate, re.I):
            return "[PRIVATE_URL_REMOVED]"
        return match[0]

    if CONTROL_TEXT.match(value):
        return None
    return NATIVE_URL.sub(replace_url, value) if redaction_enabled() else value


def observable_payload(value):
    if isinstance(value, str):
        return observable_text(value)
    if isinstance(value, list):
        return [projected for child in value if (projected := observable_payload(child)) is not None]
    if isinstance(value, dict):
        if ("type" in value and value["type"] not in VISIBLE_PAYLOAD_TYPES
                or "role" in value and value["role"] not in {"user", "assistant", "tool"}):
            return None
        return {key: projected for key, child in value.items() if key in PAYLOAD_FIELDS
                and (projected := observable_payload(child)) is not None}
    if value is None or isinstance(value, (bool, int, float)):
        return value
    raise ValueError("Unsupported native observable payload")


def observable_arguments(value):
    if isinstance(value, dict):
        if (isinstance(value.get("role"), str) and value["role"] in {"system", "control"}
                or isinstance(value.get("type"), str) and re.search(r"^(?:ui[.]|(?:elicitation|canvas|reasoning|thinking|system|control)(?:[._-]|$)|assistant[.](?:reasoning|thinking))", value["type"], re.I)):
            return OMITTED_PAYLOAD
        result = {}
        for key, child in value.items():
            normalized = re.sub(r"[^a-z]", "", str(key).lower())
            if (key in HIDDEN_FIELDS or redaction_enabled() and SECRET_FIELD.fullmatch(str(key))
                    or normalized.startswith(("canvas", "elicitation", "reasoning", "thinking"))
                    or normalized.startswith("private") and redaction_enabled()
                    or normalized in {"private", "privatemetadata", "privateenvelope", "privatecontrol"}
                    or normalized in {"system", "systemprompt", "systemmessage", "systeminstructions", "control", "controldata", "controlmessage", "controlpayload"}
                    or redaction_enabled() and normalized.endswith("token")):
                continue
            projected = observable_arguments(child)
            if projected is not OMITTED_PAYLOAD:
                result[key] = projected
        return result
    if isinstance(value, list):
        return [projected for child in value if (projected := observable_arguments(child)) is not OMITTED_PAYLOAD]
    projected = observable_payload(value)
    return OMITTED_PAYLOAD if projected is None and value is not None else projected


def observable_events(events, session_id):
    bound = False
    private_calls = set()
    for event in events:
        data = event.get("data", {})
        for owner in (event, data):
            if "sessionId" in owner and session_uuid(owner["sessionId"]) != session_id:
                raise ValueError("Snapshot belongs to a different session")
        if event.get("type") == "session.start":
            if session_uuid(data.get("sessionId")) != session_id:
                raise ValueError("Snapshot belongs to a different session")
            bound = True
        if isinstance(data.get("toolName"), str) and PRIVATE_TOOL.search(data["toolName"]):
            if isinstance(data.get("toolCallId"), str):
                private_calls.add(data["toolCallId"])
    if not bound:
        raise ValueError("Snapshot requires a matching session.start UUID")
    visible = []
    for event in events:
        kind, data = event.get("type"), dict(event.get("data", {}))
        if not isinstance(kind, str) or kind not in EVENT_FIELDS:
            continue
        attribution = {}
        for key in ATTRIBUTION_FIELDS:
            if key in event:
                if not isinstance(event[key], str):
                    raise ValueError("Invalid native event attribution")
                attribution[key] = event[key]
                if not data.get(key):
                    data[key] = event[key]
            if key in data and not isinstance(data[key], str):
                raise ValueError("Invalid native event attribution")
        source = data.get("source", "")
        if kind in {"user.message", "assistant.message"} and source and source not in VISIBLE_SOURCES and not source.startswith(("agent-", "skill-")):
            continue
        if data.get("parentAgentTaskId") and origin_of({"data": data}, session_id) not in {"delegated", "injected"}:
            data["source"] = "agent-task"
        if kind.startswith("tool.") and (data.get("toolCallId") in private_calls
                or isinstance(data.get("toolName"), str) and PRIVATE_TOOL.search(data["toolName"])):
            continue
        projected = {}
        for key, value in data.items():
            if key not in EVENT_FIELDS[kind] | ATTRIBUTION_FIELDS:
                continue
            if key in {"arguments", "result", "error"}:
                payload = observable_arguments(value)
                if payload is OMITTED_PAYLOAD:
                    continue
            else:
                payload = observable_payload(value)
                if payload is None:
                    continue
            projected[key] = payload
        if kind in {"user.message", "assistant.message", "session.imported_context"} and not projected.get("content"):
            continue
        if kind == "session.start":
            projected["sessionId"] = session_id
        visible.append({**attribution, **{key: observable_text(event[key]) for key in ("id", "parentId", "timestamp") if isinstance(event.get(key), str)},
                        "type": kind, "data": projected})
    return visible


def phase_checkpoint(directory, relative):
    path = Path(directory)
    parts = Path(relative).parts
    for index, part in enumerate((None, *parts)):
        if part is not None:
            path = path / part
        try:
            details = path.lstat()
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(details.st_mode) or getattr(details, "st_file_attributes", 0) & 0x400:
            raise OSError("Linked progress checkpoint refused")
        if index < len(parts):
            if not stat.S_ISDIR(details.st_mode):
                raise OSError("Invalid progress checkpoint directory")
        elif not stat.S_ISREG(details.st_mode):
            raise OSError("Invalid progress checkpoint file")
    return path if details.st_size else None


def native_preparation_phase(directory, privacy_mode):
    try:
        fast_support = "abstraction/story/_support"
        fast_attempt = phase_checkpoint(directory, fast_support + "/fast-attempt.json")
        if phase_checkpoint(directory, "abstraction/abstraction.json"):
            return "privacy" if privacy_mode == "llm" else ("checking" if fast_attempt else None)
        if fast_attempt is not None:
            with fast_attempt.open("rb") as stream:
                payload = stream.read(2 * 1024 * 1024 + 1)
            if len(payload) > 2 * 1024 * 1024:
                return None
            receipt = json.loads(payload)
            attempts = receipt.get("attempts") if isinstance(receipt, dict) else None
            if not isinstance(attempts, list) or len(attempts) > 2:
                return None
            indices = [entry.get("index") if isinstance(entry, dict) else None for entry in attempts]
            if indices not in ([], [0], [0, 1]) or any(type(index) is not int for index in indices):
                return None
            if indices == [0, 1] and not phase_checkpoint(directory, fast_support + "/fast-candidate-1.json"):
                return "repair"
            if any(phase_checkpoint(directory, fast_support + f"/fast-candidate-{index}.json") for index in indices):
                return "checking"
            return "draft"
        work = "abstraction/story/_support/.work"
        if not phase_checkpoint(directory, work + "/joint-draft.json"):
            return "draft" if Path(directory).is_dir() else None
        if phase_checkpoint(directory, work + "/edition-receipt.json"):
            return "checking"
        attempt = phase_checkpoint(directory, work + "/edition-attempt.json")
        if attempt is None:
            return "checking"
        with attempt.open("rb") as stream:
            payload = stream.read(2 * 1024 * 1024 + 1)
        if len(payload) > 2 * 1024 * 1024:
            return None
        receipt = json.loads(payload)
        if not isinstance(receipt, dict) or receipt.get("status") != "running":
            return None
        counts = receipt.get("repair_counts")
        repairs = counts.get("patches") if isinstance(counts, dict) else None
        if type(repairs) is not int or not 0 <= repairs <= 100:
            return None
        if not repairs:
            return "checking"
        completed = 0
        for position, patch in enumerate(attempt.parent.glob("edition-patch-*.json")):
            if position >= 100:
                return None
            if re.fullmatch(r"edition-patch-[0-9]+\.json", patch.name) and phase_checkpoint(directory, work + "/" + patch.name):
                completed += 1
        return "repair" if completed < repairs else "checking"
    except (OSError, ValueError, TypeError, RecursionError):
        return None


class NativeStudio(DurableStudio):
    def __init__(self, *arguments, **settings):
        self.worker_lock = threading.Lock()
        self.workers = set()
        self.preparation_limits = {"total_seconds": bounded_seconds(settings.pop("preparation_timeout", 290), 290, "preparation_timeout"),
                                   "call_seconds": settings.pop("model_timeout", 120),
                                   "max_calls": settings.pop("preparation_max_calls", 3)}
        PreparationBudget(**self.preparation_limits)
        super().__init__(*arguments, **settings)

    def _action(self, path, data):
        if path != "/api/scan":
            return super()._action(path, data)
        if data.get("pipeline") != PIPELINE:
            raise ValueError("Explicit abstract-first preparation consent is required")
        host_model = data.get("host_model")
        if host_model is not None and (not isinstance(host_model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,159}", host_model)):
            raise ValueError("Invalid active-session model identifier")
        settings = dict(self.settings)
        if not settings.get("model") and host_model is not None:
            settings["model"] = host_model
        selection = preferences(data.get("readers"), data.get("delivery"), data.get("audience"))
        mode = data.get("privacy_mode")
        if mode == "full":
            if data.get("detection") != "none" or data.get("semantic") is not False:
                raise ValueError("No-redaction mode must explicitly skip privacy scanning")
        elif mode != "llm" or data.get("detection") != "copilot" or data.get("semantic") is not True:
            raise ValueError("Smart redaction requires both local rules and Copilot review")
        source = next((item for item in self.sessions if item["id"] == data.get("session")), None)
        if source is None:
            raise ValueError("Select this conversation's explicit snapshot")
        identifier = uuid.uuid4().hex
        job = {"id": identifier, "status": "new", "stage": "scan", "directory": self.output / identifier,
               "pipeline": PIPELINE, "privacy_mode": mode}
        job["model_selection"] = {"requested": settings.get("model") or "copilot-default",
                                  "source": "configured" if self.settings.get("model") else "host" if host_model else "cli-default"}
        job["directory"].mkdir()
        self.jobs[identifier] = job

        def prepare():
            with preparation_budget(**self.preparation_limits) as budget:
                try:
                    review = prepare_abstract_review(source["path"], self.home, job["directory"] / "review", data["audience"],
                                                     selection, mode, custom=data.get("custom", []), fast=True, **settings)
                    budget.check()
                finally:
                    failed = sys.exc_info()[0] is not None
                    try:
                        write_json(job["directory"] / "preparation-budget.json", budget.receipt())
                    except OSError:
                        with self.lock:
                            job["budget_receipt_error"] = "write_failed"
                        if not failed:
                            raise
                    finally:
                        with self.lock:
                            job["automated_elapsed_seconds"] = time.monotonic() - budget.started
            return {"findings": len(review["findings"]), "review_id": review["review_id"], "pipeline": PIPELINE}

        return self.background(job, "scan", prepare)

    def background(self, job, stage, operation):
        def tracked_operation():
            with self.worker_lock:
                self.workers.add(threading.current_thread())
            with self.lock:
                job.pop("error_code", None)
            try:
                if stage == "generate" and job.get("pipeline") == PIPELINE:
                    spent = job.get("automated_elapsed_seconds")
                    if isinstance(spent, bool) or not isinstance(spent, (int, float)) or not 0 <= spent < 300:
                        raise PreparationTimeoutError()
                    remaining = min(10, 300 - spent)
                    with preparation_budget(total_seconds=remaining, call_seconds=remaining, max_calls=0) as budget:
                        try:
                            return operation()
                        finally:
                            failed = sys.exc_info()[0] is not None
                            try:
                                write_json(job["directory"] / ("export-budget-" + uuid.uuid4().hex + ".json"), budget.receipt())
                            except OSError:
                                if not failed:
                                    raise
                            finally:
                                with self.lock:
                                    job["automated_elapsed_seconds"] = spent + time.monotonic() - budget.started
                return operation()
            except (PreparationTimeoutError, PreparationCallLimitError) as error:
                with self.lock:
                    job["error_code"] = error.error_code
                raise

        with self.action_lock:
            if getattr(self, "closing", False):
                raise ValueError("Native bridge is closing")
            previous_status = job.get("status")
            try:
                return super().background(job, stage, tracked_operation)
            except BaseException:
                with self.lock:
                    if previous_status != "running" and job.get("status") == "running":
                        job.update(status="error", error="Native worker did not start; inspect the private checkpoint before retrying")
                raise

    def drain(self):
        while True:
            with self.lock:
                running = any(job["status"] == "running" for job in self.jobs.values())
            with self.worker_lock:
                workers = tuple(self.workers)
            for worker in workers:
                worker.join(0.05)
            if not running and not any(worker.is_alive() for worker in workers):
                return
            time.sleep(0.05)

    def public_progress(self, identifier):
        with self.lock:
            job = self.job(identifier)
            stage, status = job["stage"], job["status"]
            phase = "working"
            if status == "error":
                phase = "error"
            elif stage == "scan":
                phase = "review" if status == "done" else native_preparation_phase(job["directory"], job.get("privacy_mode")) or "draft"
            elif stage == "generate":
                phase = "ready" if status == "done" else "export"
            elif stage in {"publish", "verify"}:
                phase = "ready"
            elapsed = job.get("automated_elapsed_seconds", 0)
            if stage == "scan" and status == "running":
                start = next((entry.get("at") for entry in job.get("history", []) if entry.get("stage") == "scan" and entry.get("status") == "running"), None)
                if start:
                    try:
                        elapsed = time.time() - datetime.fromisoformat(start.replace("Z", "+00:00")).timestamp()
                    except (ValueError, TypeError):
                        elapsed = 0
            result = {"schema": "native-progress/v1", "phase": phase, "elapsed_seconds": max(0, int(elapsed)), "limit_seconds": 300}
            if phase == "error":
                result["error_code"] = job.get("error_code") if job.get("error_code") in {"timeout", "cleanup_unconfirmed", "call_budget"} else "export_failed"
            return result

    def output_delivery(self, identifier, expected=None, name=None, local_paths=False):
        with self.action_lock, self.lock:
            job = self.job(identifier)
            if getattr(self, "closing", False) or job["status"] != "done" or job["stage"] not in {"generate", "publish", "verify"}:
                raise ValueError("Only completed selected outputs can be previewed")
            generation, snapshot = self.delivery_snapshot(job)
            if expected is not None and expected != snapshot["id"]:
                raise ValueError("Selected generation changed; request a new output preview")
            files, targets, total = [], {}, 0
            for filename, fingerprint in snapshot["files"].items():
                target = generation / filename if filename == "deliverables.zip" else generation / "deliverables" / filename
                size = target.stat().st_size
                total += size
                if total > LIMIT:
                    raise ValueError("Selected preview payload exceeds 32 MiB")
                targets[filename] = target
                files.append({"name": filename, "bytes": size, "sha256": fingerprint})
            manifest = {"schema": "native-output/v1", "job": identifier, "snapshot_id": snapshot["id"],
                        "files": [item for item in files if item["name"] != "deliverables.zip"],
                        "bundle": next(item for item in files if item["name"] == "deliverables.zip")}
            if local_paths:
                manifest["local"] = {"folder": str(generation / "deliverables"),
                                     "files": {filename: str(target) for filename, target in targets.items()}}
            if name is None:
                return manifest
            if name not in targets:
                raise ValueError("File was not selected for delivery")
            with targets[name].open("rb") as stream:
                content = stream.read(LIMIT + 1)
            if len(content) > LIMIT or hashlib.sha256(content).hexdigest() != snapshot["files"][name]:
                raise ValueError("Selected file changed during read")
            return content, snapshot["files"][name]

    def open_output(self, identifier, expected, name):
        with self.action_lock:
            if not isinstance(expected, str) or not expected:
                raise ValueError("A bound delivery snapshot is required")
            manifest = self.output_delivery(identifier, expected, local_paths=True)
            if name == "folder":
                target = Path(manifest["local"]["folder"])
            elif name in {item["name"] for item in manifest["files"]}:
                target = Path(manifest["local"]["files"][name])
            else:
                raise ValueError("Only selected reports or their output folder can be opened")
            for parent in (target, *target.parents):
                info = parent.lstat()
                if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
                    raise ValueError("Linked output paths cannot be opened")
            open_local_output(target)
            return {"status": "dispatched", "target": name}


def output_handler(studio, identifier, snapshot_id, token):
    binding_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format_string, *arguments):
            return

        def send(self, status, payload, content_type="application/json; charset=utf-8", fingerprint=None):
            content = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; frame-src 'self' blob:; object-src 'none'; base-uri 'none'; form-action 'none'")
            if fingerprint is not None:
                self.send_header("X-Content-SHA256", fingerprint)
                self.send_header("X-Delivery-Snapshot", snapshot_id)
            self.end_headers()
            self.wfile.write(content)

        def do_GET(self):
            nonlocal snapshot_id
            parsed = urllib.parse.urlsplit(self.path)
            static = {"/": ("native-output.html", "text/html; charset=utf-8"),
                      "/native-output.js": ("native-output.js", "text/javascript; charset=utf-8"),
                      "/native-output.css": ("native-output.css", "text/css; charset=utf-8")}
            expected_host = "127.0.0.1:" + str(self.server.server_port)
            if self.headers.get("Host") != expected_host:
                self.send(403, {"error": "Read-only preview origin refused"})
                return
            try:
                if parsed.path in static:
                    filename, content_type = static[parsed.path]
                    self.send(200, (WEB / filename).read_bytes(), content_type)
                    return
                if (self.headers.get("Origin") not in (None, "http://" + expected_host)
                        or not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + token)):
                    self.send(403, {"error": "Read-only preview authorization required"})
                    return
                query = urllib.parse.parse_qs(parsed.query)
                if parsed.path == "/api/progress" and not parsed.query:
                    self.send(200, studio.public_progress(identifier))
                elif parsed.path == "/api/delivery" and not parsed.query:
                    with binding_lock:
                        manifest = studio.output_delivery(identifier, snapshot_id, local_paths=True)
                        snapshot_id = manifest["snapshot_id"]
                    self.send(200, manifest)
                elif parsed.path == "/api/file" and set(query) == {"name"} and len(query["name"]) == 1:
                    if snapshot_id is None:
                        raise ValueError("No approved delivery snapshot is bound")
                    name = query["name"][0]
                    content, fingerprint = studio.output_delivery(identifier, snapshot_id, name)
                    self.send(200, content, "application/zip" if name == "deliverables.zip" else "text/plain; charset=utf-8", fingerprint)
                else:
                    self.send(404, {"error": "Only selected completed exports are available"})
            except (ValueError, OSError, KeyError, TypeError):
                self.send(409, {"error": "Selected output is unavailable or changed; request a new preview after checking job status"})

        def do_POST(self):
            if self.path != "/api/open":
                self.method_not_allowed()
                return
            expected_host = "127.0.0.1:" + str(self.server.server_port)
            if (self.headers.get("Host") != expected_host
                    or self.headers.get("Origin") != "http://" + expected_host
                    or not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + token)):
                self.send(403, {"error": "Selected output authorization required"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if (self.headers.get("Content-Type") != "application/json" or self.headers.get("Transfer-Encoding")
                        or not 0 < length <= 512):
                    raise ValueError("Expected a bounded open request")
                self.connection.settimeout(5)
                request = json.loads(self.rfile.read(length))
                if (not isinstance(request, dict) or set(request) != {"target", "snapshot_id"}
                        or not isinstance(request["target"], str)
                        or snapshot_id is None or request["snapshot_id"] != snapshot_id):
                    raise ValueError("Expected the bound selected output")
                self.send(200, studio.open_output(identifier, snapshot_id, request["target"]))
            except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError):
                self.send(409, {"error": "Could not open this saved output. Use the displayed path or download the ZIP; no new export was started."})

        def method_not_allowed(self):
            self.send(405, {"error": "This preview is read-only"})

        do_PUT = do_DELETE = do_PATCH = do_OPTIONS = method_not_allowed

    return Handler


@contextmanager
def protocol_output():
    original = sys.stdout
    protocol = original
    descriptor = None
    try:
        try:
            output_descriptor, diagnostics_descriptor = original.fileno(), sys.stderr.fileno()
        except (AttributeError, OSError, ValueError):
            output_descriptor = None
        if output_descriptor is not None:
            original.flush()
            descriptor = os.dup(output_descriptor)
            protocol = os.fdopen(descriptor, "w", encoding="utf-8", buffering=1)
            os.dup2(diagnostics_descriptor, output_descriptor)
        sys.stdout = sys.stderr
        yield protocol
    finally:
        sys.stdout.flush()
        if descriptor is not None:
            os.dup2(descriptor, output_descriptor)
            protocol.close()
        sys.stdout = original


class NativeBridge:
    def __init__(self, session_id, root=None, settings=None):
        self.session_id = session_uuid(session_id)
        self.root = Path(root or runtime_root().parent.parent / "native" / runtime_root().name / self.session_id).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lease = FileLease(self.root / "native.lock")
        self.studio = self.server = self.thread = None
        self.preview = None
        self.leased = self.closed = False
        self.close_lock = threading.Lock()
        self.sources = set()
        self.source_modes = {}
        try:
            self.lease.__enter__()
            self.leased = True
            self.studio = NativeStudio(self.root / "no-session-discovery", self.root / "jobs", **(settings or {}))
        except BaseException:
            self.close()
            raise

    def close(self):
        with self.close_lock:
            if self.closed:
                return
            if self.studio is not None:
                with self.studio.action_lock:
                    self.studio.closing = True
            self.stop_canvas()
            if self.studio is not None:
                self.studio.drain()
            if self.leased:
                self.lease.__exit__(None, None, None)
                self.leased = False
            self.closed = True

    def stop_canvas(self):
        if self.thread is not None and self.thread.is_alive():
            self.server.shutdown()
            self.thread.join()
        if self.server is not None:
            self.server.server_close()
        self.server = self.thread = None
        self.preview = None

    def output_canvas(self, identifier):
        with self.close_lock:
            manifest = self.studio.output_delivery(identifier)
            if self.preview and self.preview["identifier"] == identifier and self.thread is not None and self.thread.is_alive():
                return {**manifest, "url": self.preview["url"], "read_only": True}
            self.stop_canvas()
            token = "readOnly." + secrets.token_urlsafe(32)
            try:
                self.server = ThreadingHTTPServer(("127.0.0.1", 0), output_handler(self.studio, identifier, manifest["snapshot_id"], token))
                self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
                self.thread.start()
            except BaseException:
                self.stop_canvas()
                raise
            fragment = urllib.parse.urlencode({"access": token})
            return {**manifest, "url": f"http://127.0.0.1:{self.server.server_port}/#{fragment}", "read_only": True}

    def progress_canvas(self, identifier):
        with self.close_lock:
            self.studio.public_progress(identifier)
            if self.preview and self.preview["identifier"] == identifier and self.thread is not None and self.thread.is_alive():
                return {"url": self.preview["url"], "read_only": True}
            self.stop_canvas()
            token = "readOnly." + secrets.token_urlsafe(32)
            try:
                self.server = ThreadingHTTPServer(("127.0.0.1", 0), output_handler(self.studio, identifier, None, token))
                self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
                self.thread.start()
            except BaseException:
                self.stop_canvas()
                raise
            fragment = urllib.parse.urlencode({"access": token, "progress": "1"})
            url = f"http://127.0.0.1:{self.server.server_port}/#{fragment}"
            self.preview = {"identifier": identifier, "url": url}
            return {"url": url, "read_only": True}

    def capture(self, events, privacy_mode="llm"):
        if privacy_mode not in {"full", "llm"}:
            raise ValueError("Choose no redaction or smart redaction before capture")
        with self.studio.action_lock:
            if getattr(self.studio, "closing", False):
                raise ValueError("Native bridge is closing")
            return self._capture(events, privacy_mode)

    def _capture(self, events, privacy_mode):
        if not isinstance(events, list) or not events or len(events) > 100000:
            raise ValueError("Expected a nonempty bounded current-session event snapshot")
        if any(not isinstance(event, dict) or not isinstance(event.get("data", {}), dict) for event in events):
            raise ValueError("Malformed current-session event")
        if len(json.dumps(events, ensure_ascii=False, allow_nan=False).encode()) > LIMIT:
            raise ValueError("Current conversation exceeds the 32 MiB review limit; nothing was truncated")
        with content_redaction(False):
            visible = observable_events(events, self.session_id)
            reduced = sanitize(visible)
        turns = sum(event.get("type") == "user.message" and origin_of(event, self.session_id) == "root" for event in reduced)
        if not turns:
            raise ValueError("The current conversation has no root user messages to export")
        directory = self.root / "snapshots" / uuid.uuid4().hex
        directory.mkdir(parents=True, mode=0o700)
        source = directory / "events.jsonl"
        payload = "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in reduced).encode("utf-8")
        source.write_bytes(payload)
        fingerprint = hashlib.sha256(payload).hexdigest()
        write_json(directory / "capture.json", {"schema": "native-session-capture/v2", "session_id": self.session_id,
                   "events": len(reduced), "source_sha256": fingerprint, "source": "Copilot SDK getEvents",
                   "hard_removals": {"secret_fields": 0, "secret_pattern_matches": 0}, "capture_order": PIPELINE,
                   "privacy_mode": privacy_mode, "hidden_events_removed": len(events) - len(visible),
                   "capture_policy": "observable-events/v1", "fields": "Allowlisted observable fields only; unknown/control metadata omitted"})
        selector = self.studio.select_source(str(source))
        for item in self.studio.sessions:
            if item["id"] == selector:
                item["title"] = f"Current conversation · {turns} user messages"
        self.sources.add(selector)
        self.source_modes[selector] = privacy_mode
        return {"source": selector, "session_id": self.session_id, "events": len(reduced), "user_messages": turns,
                "sha256": fingerprint, "privacy_mode": privacy_mode, "capture_order": PIPELINE}

    def bound_review(self, job):
        directory = job["directory"] / "review"
        if directory.is_symlink() or (directory / "review.json").is_symlink():
            raise ValueError("Linked saved review refused")
        stored = json.loads((directory / "review.json").read_bytes())
        if stored.get("pipeline") != PIPELINE:
            raise ValueError("Saved review predates abstract-first consent; start a new snapshot")
        if Path(stored["source_path"]) != directory.parent / "abstraction/source/events.jsonl":
            raise ValueError("Saved review does not reference this job's abstract document")
        manifest_path = directory.parent / "abstraction/abstraction.json"
        if manifest_path.is_symlink() or manifest_path.parent.is_symlink():
            raise ValueError("Linked abstraction refused")
        manifest = json.loads(manifest_path.read_bytes())
        source = Path(manifest["source_path"])
        snapshots = self.root / "snapshots"
        capture = source.parent / "capture.json"
        if (source.name != "events.jsonl" or source.parent.parent != snapshots or snapshots.is_symlink()
                or source.is_symlink() or source.parent.is_symlink() or capture.is_symlink()
                or not source.resolve().is_relative_to(snapshots.resolve())):
            raise ValueError("Saved review does not belong to this session snapshot directory")
        binding = json.loads(capture.read_bytes())
        if (binding.get("schema") != "native-session-capture/v2" or binding.get("capture_order") != PIPELINE
                or binding.get("session_id") != self.session_id or binding.get("source_sha256") != manifest.get("source_sha256")):
            raise ValueError("Saved review snapshot identity changed")
        review, baseline = load_review(directory)
        verified = load_abstract_review(directory, review)
        if verified != manifest:
            raise ValueError("Saved abstraction changed during validation")
        mode = review.get("privacy_mode")
        if mode not in {"full", "llm"} or binding.get("privacy_mode") != mode:
            raise ValueError("Saved review privacy mode does not match the captured snapshot")
        projected = present_review(review, baseline)
        counts = binding.get("hard_removals")
        if counts != {"secret_fields": 0, "secret_pattern_matches": 0} or any(type(value) is not int for value in counts.values()):
            raise ValueError("Saved capture rule summary is invalid")
        projected["capture_rule_matches"] = counts
        projected["original_source_sha256"] = verified["source_sha256"]
        return projected

    def pending_review(self):
        with self.studio.action_lock:
            candidates = sorted(self.studio.jobs.values(), key=lambda job: job.get("updated_at", ""), reverse=True)
            legacy = 0
            for job in candidates:
                if job.get("status") != "done" or job.get("stage") != "scan" or "generation" in job:
                    continue
                directory = job["directory"] / "review"
                if directory.is_symlink() or (directory / "review.json").is_symlink():
                    raise ValueError("Linked saved review refused")
                stored = json.loads((directory / "review.json").read_bytes())
                if "pipeline" not in stored and "abstraction_sha256" not in stored and "pipeline" not in job:
                    legacy += 1
                    continue
                review = self.bound_review(job)
                mode = review.get("privacy_mode", "llm")
                if mode == "llm" and review.get("semantic", {}).get("status") != "reviewed":
                    continue
                selected = preferences(review["preferences"].get("readers"), review["preferences"].get("destination"), review["audience"])
                return {"review": {"job": job["id"], "review_id": review["review_id"], "findings": len(review["findings"]),
                                   "updated_at": job.get("updated_at"), "readers": selected["readers"],
                                   "delivery": selected["destination"], "audience": review["audience"],
                                   "snapshot_sha256": review["original_source_sha256"], "privacy_mode": mode,
                                   "review_source_sha256": review["source_sha256"], "pipeline": PIPELINE,
                                   "abstraction_sha256": review["abstraction_sha256"]}}
            return {"review": None, **({"legacy_reviews_unavailable": legacy} if legacy else {})}

    def call(self, operation, data):
        if not isinstance(data, dict):
            raise ValueError("Expected object arguments")
        if operation == "capture":
            if data.get("capture_order") != PIPELINE or data.get("privacy_mode") not in {"full", "llm"}:
                raise ValueError("Explicit abstract-first capture order and privacy mode are required")
            return self.capture(data.get("events"), data["privacy_mode"])
        if operation == "pending_review":
            if data:
                raise ValueError("Saved review selection takes no paths or session arguments")
            return self.pending_review()
        if operation == "scan":
            if data.get("session") not in self.sources:
                raise ValueError("Only this extension's explicit current-session snapshot can be reviewed")
            if data.get("privacy_mode") not in {"full", "llm"}:
                raise ValueError("An explicit native privacy mode is required")
            if data["privacy_mode"] != self.source_modes[data["session"]]:
                raise ValueError("Privacy mode changed after capture; take a new explicit snapshot")
            return self.studio.action("/api/scan", data)
        identifier = data.get("job")
        if not isinstance(identifier, str) or not re.fullmatch(r"[a-f0-9]{32}", identifier):
            raise ValueError("Invalid native job")
        job = self.studio.job(identifier)
        if operation == "output_canvas":
            return self.output_canvas(identifier)
        if operation == "progress_canvas":
            return self.progress_canvas(identifier)
        if operation == "status":
            result = self.studio.snapshot(identifier)
            if result.get("status") == "error" and job.get("error_code") in {"timeout", "cleanup_unconfirmed", "call_budget"}:
                result["error_code"] = job["error_code"]
                if job["error_code"] == "cleanup_unconfirmed":
                    result["next_action"] = "Cleanup is unconfirmed. Do not retry until the local job is checked."
            if result.get("stage") == "scan" and result.get("status") == "running" and job.get("pipeline") == PIPELINE:
                phase = native_preparation_phase(job["directory"], job.get("privacy_mode"))
                if phase in {"draft", "checking", "repair", "privacy"}:
                    result["phase"] = phase
            return result
        if operation == "review":
            if job["status"] != "done" or job["stage"] != "scan":
                raise ValueError("Wait for completed privacy review")
            return self.bound_review(job)
        if operation == "deliverables":
            return self.studio.delivered(identifier)
        if operation == "generate":
            if data.get("pipeline") != PIPELINE:
                raise ValueError("Abstract-first render/export confirmation is required")
            review = self.bound_review(job)
            if data.get("abstraction_sha256") != review["abstraction_sha256"]:
                raise ValueError("Approved abstraction changed before render/export")
            if review.get("privacy_mode") not in {"full", "llm"}:
                raise ValueError("Native export requires an explicit privacy mode")
            if review["privacy_mode"] == "llm" and review.get("semantic", {}).get("status") != "reviewed":
                raise ValueError("Smart redaction requires completed Copilot review; local rules alone are not sufficient")
        if operation in {"generate", "package", "plan", "publish", "verify"}:
            return self.studio.action("/api/" + operation, copy.deepcopy(data))
        raise ValueError("Unsupported native operation")


def native_error(bridge, operation, error):
    from .artifacts import ArtifactAuthError, ArtifactError, ArtifactExistsError, ArtifactNameError
    code = "operation_failed"
    if isinstance(error, ArtifactNameError):
        code = "artifact_name_invalid"
    elif isinstance(error, ArtifactExistsError):
        code = "artifact_exists"
    elif isinstance(error, ArtifactAuthError) or isinstance(error, ArtifactError) and error.status in {401, 403}:
        code = "artifact_auth_required"
    elif operation == "plan":
        code = "artifact_plan_failed"
    elif operation == "publish":
        code = "publication_unconfirmed"
    try:
        directory = bridge.root / "diagnostics"
        directory.mkdir(exist_ok=True, mode=0o700)
        write_json(directory / (uuid.uuid4().hex + ".json"), {"operation": operation if isinstance(operation, str) and operation in {
            "capture", "pending_review", "scan", "status", "review", "generate", "deliverables", "output_canvas", "progress_canvas", "package", "plan", "publish", "verify"
        } else "unknown", "code": code, "error_type": type(error).__name__, "message": str(error)})
    except OSError:
        pass
    return {"error": "Native operation refused or failed. No source or private diagnostic text was sent to the model.", "error_code": code}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--session", required=True)
    arguments = parser.parse_args()
    with protocol_output() as responses:
        from .cli import local_defaults
        defaults = local_defaults()
        settings = {key: defaults[key] for key in ("model", "gh_host", "max_calls", "preparation_timeout", "model_timeout", "preparation_max_calls") if key in defaults}
        bridge = NativeBridge(arguments.session, settings=settings)
        try:
            while True:
                line = sys.stdin.buffer.readline(LIMIT + 65537)
                if not line:
                    return
                if len(line) > LIMIT + 65536:
                    raise ValueError("Native request exceeds the input limit")
                request_id = None
                operation = None
                try:
                    request = json.loads(line)
                    request_id = request["id"]
                    operation = request["operation"]
                    result = bridge.call(operation, request.get("data", {}))
                    response = {"id": request_id, "result": result}
                except Exception as error:
                    response = {"id": request_id, **native_error(bridge, operation, error)}
                responses.write(json.dumps(response, ensure_ascii=False) + "\n")
                responses.flush()
        finally:
            bridge.close()
