import copy
import hmac
import hashlib
import json
import os
import secrets
import subprocess
import sys
import threading
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .artifacts import ArtifactClient, destination_plan, publish_package
from .ingest import list_sessions, resolve_session
from .private_cli import generate_private
from .reduction import digest, load_review, scan_session, transform, prepare_full_session
from .reduction_semantic import semantic_review
from .privacy_presentation import present_review
from .share_package import approve_package, load_package, prepare_package
from .story_pipeline import validate_story
from .delivery import filenames, preferences, validate_delivery


WEB = Path(__file__).parent / "web"


def open_local(path):
    if os.name == "nt":
        os.startfile(str(path))
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


class Studio:
    durable_jobs = False

    def __init__(self, home, output, session=None, open_generation=None, review_directory=None, discover_sessions=True, **settings):
        self.home = Path(home).resolve()
        self.output = Path(output).resolve()
        self.output.mkdir(parents=True, exist_ok=True)
        self.token = secrets.token_urlsafe(32)
        self.settings = settings
        if session:
            source = resolve_session(session, self.home)
            self.sessions = [{"id": "selected", "title": source.parent.name, "path": str(source), "bytes": source.stat().st_size}]
        elif discover_sessions:
            self.sessions = list_sessions(self.home, limit=100)
        else:
            self.sessions = []
        self.jobs = {}
        self.lock = threading.Lock()
        self.action_lock = threading.RLock()
        self.resume_job = None
        self.resume_choices = None
        if open_generation or review_directory:
            if not open_generation or not review_directory:
                raise ValueError("Reopening a generation requires both --open-generation and --review")
            generation = Path(open_generation).resolve()
            review_directory = Path(review_directory).resolve()
            review, baseline = load_review(review_directory)
            decisions = json.loads((review_directory / "decisions.json").read_bytes())
            receipt = json.loads((generation / "reduced/reduction.json").read_bytes())
            if receipt.get("review_id") != review["review_id"] or receipt.get("decisions_sha256") != digest(decisions):
                raise ValueError("Saved choices differ from this generation; open a matching review")
            if not validate_story(generation / "story")["valid"]:
                raise ValueError("Only validated finished outputs can be reopened; use private-story --resume for a failed generation")
            selection = review.get("preferences", {})
            preferences(selection.get("readers"), selection.get("destination"), review["audience"])
            validate_delivery(generation, selection)
            identifier = uuid.uuid4().hex
            self.jobs[identifier] = {"id": identifier, "directory": review_directory.parent, "review_directory": review_directory,
                                     "generation": generation, "decision_id": digest(decisions), "status": "done", "stage": "generate",
                                     "approved_choices": decisions,
                                     "result": {"output": str(generation / "deliverables"), "files": filenames(selection["readers"]), "preferences": selection}}
            self.resume_job = identifier
            self.resume_choices = decisions["choices"]

    def background(self, job, stage, operation):
        with self.lock:
            if any(item["status"] == "running" for item in self.jobs.values()):
                raise ValueError("Another operation is running; wait for it to finish")
            job.update(status="running", stage=stage, error=None)
            self.changed(job)

        def worker():
            try:
                result = operation()
                with self.lock:
                    job.update(status="done", result=result)
                    if stage == "generate":
                        job["delivery_result"] = result
                    self.changed(job)
            except Exception as error:
                with self.lock:
                    job.update(status="error", error=str(error)[:1200])
                    if stage == "generate":
                        job["error"] += " Retry with unchanged choices to reuse the private drafting checkpoint; changing choices starts a new generation."
                    self.changed(job)

        threading.Thread(target=worker, daemon=True).start()
        return {"job": job["id"]}

    def changed(self, job):
        pass

    def job(self, identifier):
        if identifier not in self.jobs:
            raise ValueError("Unknown job in this studio session")
        return self.jobs[identifier]

    def delivery_snapshot(self, job):
        if "generation" not in job or job["status"] == "running":
            raise ValueError("Only completed deliverable files are available")
        generation = Path(job["generation"]).resolve()
        review, baseline = load_review(job.get("review_directory", job["directory"] / "review"))
        if job.get("approved_choices") and job.get("decision_id") != digest(job["approved_choices"]):
            raise ValueError("Privacy choices changed; regenerate before downloading")
        selection = review.get("preferences", {})
        manifest = validate_delivery(generation, selection)
        if manifest.get("review_id") != review["review_id"]:
            raise ValueError("Delivery review identity changed")
        identity = digest({"generation": str(generation), "decision_id": job.get("decision_id"), "manifest": manifest})
        return generation, {"id": identity, "files": {**manifest["files"], "deliverables.zip": manifest["archive_sha256"]}}

    def action(self, path, data):
        with self.action_lock:
            if getattr(self, "closing", False):
                raise ValueError("Runtime is stopping; reopen it before starting work")
            if any(item["status"] == "running" for item in self.jobs.values()):
                raise ValueError("Another operation is running; wait for it to finish")
            result = self._action(path, data)
            if data.get("job") in self.jobs:
                self.changed(self.jobs[data["job"]])
            return result

    def _action(self, path, data):
        if path == "/api/teams":
            result = ArtifactClient().teams()
            return {"teams": result.get("teams", []) if isinstance(result, dict) else result}
        if path == "/api/scan":
            selection = preferences(data.get("readers"), data.get("delivery"), data.get("audience"))
            privacy_mode = data.get("privacy_mode")
            if privacy_mode not in {None, "full", "llm"}:
                raise ValueError("Choose no redaction or smart redaction")
            if privacy_mode == "full":
                if data.get("detection") != "none" or data.get("semantic") is not False:
                    raise ValueError("No-redaction mode must explicitly skip privacy scanning")
            elif data.get("detection") not in {"local", "copilot"}:
                raise ValueError("Explicitly choose local detection or a contextual Copilot review")
            if (data.get("detection") == "copilot") != (data.get("semantic") is True):
                raise ValueError("Contextual Copilot review requires separate disclosure consent")
            if privacy_mode == "llm" and data.get("semantic") is not True:
                raise ValueError("Smart redaction requires both local rules and Copilot review")
            source = next((item for item in self.sessions if item["id"] == data.get("session")), None)
            if not source:
                raise ValueError("Select one of the listed sessions")
            identifier = uuid.uuid4().hex
            job = {"id": identifier, "status": "new", "stage": "scan", "directory": self.output / identifier}
            job["directory"].mkdir()
            self.jobs[identifier] = job

            def scan():
                if privacy_mode == "full":
                    review = prepare_full_session(source["path"], self.home, job["directory"] / "review", data["audience"], selection)
                else:
                    review = scan_session(source["path"], self.home, job["directory"] / "review", audience=data.get("audience", "local"),
                                          purpose=data.get("purpose", "Technical story and actionable Agent handoff"), custom=data.get("custom", []),
                                          preferences=selection, privacy_mode=privacy_mode)
                if data.get("semantic") is True:
                    review = semantic_review(job["directory"] / "review", consent=True, **self.settings)
                return {"findings": len(review["findings"]), "review_id": review["review_id"]}

            return self.background(job, "scan", scan)
        job = self.job(data.get("job"))
        directory = job["directory"]
        if path == "/api/open":
            name = data.get("name")
            if name not in {"folder", "agent-spec.md", "evidence.md"} or job["status"] != "done" or "generation" not in job:
                raise ValueError("Only a completed output folder or Markdown companion can be opened")
            review, baseline = load_review(job.get("review_directory", directory / "review"))
            selection = review.get("preferences", {})
            if job.get("approved_choices") and job.get("decision_id") != digest(job["approved_choices"]):
                raise ValueError("Privacy choices changed; regenerate before opening files")
            if name != "folder" and name not in filenames(selection.get("readers")):
                raise ValueError("This file was not selected for delivery")
            validate_delivery(job["generation"], selection)
            target = job["generation"] / "deliverables"
            if name != "folder":
                target = target / name
            if not target.exists() or target.is_symlink():
                raise ValueError("Output is missing or linked; inspect the local directory")
            open_local(target)
            return {"requested": str(target), "note": "Requested the operating system's default file app; no upload or historical command execution."}
        if path in {"/api/choices", "/api/generate"}:
            if data.get("confirmed") is not True:
                raise ValueError("Confirm which details will be kept or reduced before generation")
            review_directory = job.get("review_directory", directory / "review")
            review, baseline = load_review(review_directory)
            decisions = {"review_id": data.get("review_id"), "audience": review["audience"], "choices": data.get("choices")}
            transform(review, baseline, decisions)
            job["approved_choices"] = decisions
            if path == "/api/choices":
                if job.get("decision_id") != digest(decisions):
                    job.pop("package", None)
                    job.pop("plan", None)
                return {"job": job["id"], "approved": True, "note": "Choices saved locally. Copilot may now start generation, but not upload."}
            retry = job.get("decision_id") == digest(decisions) and "generation" in job
            if retry and job["status"] == "done" and "delivery_result" in job:
                validate_delivery(job["generation"], review["preferences"])
                return {"job": job["id"], "completed": True}
            generation = job["generation"] if retry else directory / ("generation-" + uuid.uuid4().hex)
            job["generation"] = generation
            job["decision_id"] = digest(decisions)
            job.pop("delivery_result", None)
            job.pop("package", None)
            job.pop("plan", None)

            def generate():
                return generate_private(review_directory, decisions, generation, self.home, resume=retry, confirm_choices=True, **self.settings)

            return self.background(job, "generate", generate)
        if path == "/api/package":
            if job["status"] != "done" or "generation" not in job:
                raise ValueError("Finish generation before preparing a share package")
            review, baseline = load_review(job.get("review_directory", directory / "review"))
            selection = review.get("preferences", {})
            if job.get("approved_choices") and job.get("decision_id") != digest(job["approved_choices"]):
                raise ValueError("Privacy choices changed; regenerate before preparing a package")
            if selection.get("destination") != "artifactstore":
                raise ValueError("You selected local files, not ArtifactStore; start a new destination review to upload")
            if "package" in job:
                return load_package(job["package"])
            package = directory / ("package-" + uuid.uuid4().hex)
            manifest = prepare_package(job["generation"] / "story", package, review["audience"], data.get("evidence") is not False, selection.get("readers"))
            job["package"] = package
            job.pop("plan", None)
            return manifest
        if path == "/api/approve":
            if "package" not in job:
                raise ValueError("Prepare a current package first")
            return approve_package(job["package"], data.get("package_id"), data.get("acknowledged", []), data.get("reviewed_all_files") is True)
        if path == "/api/plan":
            plan = destination_plan(job["package"], data.get("site", ""), ArtifactClient(), preview=True)
            job["plan"] = plan
            return plan
        if path == "/api/verify":
            if "plan" not in job or "package" not in job:
                raise ValueError("No recorded publication to verify")
            from .artifacts import verify_publication
            return self.background(job, "verify", lambda: verify_publication(job["package"], job["plan"], ArtifactClient()))
        if path == "/api/publish":
            if "plan" not in job:
                raise ValueError("Review a destination plan before publishing")
            if (job["package"] / "publication.json").exists():
                raise ValueError("Publication already attempted; verify this artifact instead of retrying the write")
            if data.get("confirm") != job["plan"].get("plan_id"):
                raise ValueError("Destination changed; inspect the current destination before uploading")
            if data.get("publish_intent") is True:
                approve_package(job["package"], data.get("package_id"), data.get("acknowledged", []), confirmed_publish=True)
            return self.background(job, "publish", lambda: publish_package(job["package"], job["plan"], data.get("confirm"), ArtifactClient()))
        raise ValueError("Unknown studio action")


def handler_for(studio):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format_string, *arguments):
            return

        def send(self, status, body, content_type="application/json; charset=utf-8", snapshot=None):
            payload = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            if snapshot is not None:
                self.send_header("X-Delivery-Snapshot", snapshot)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-src 'self' blob:; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(payload)

        def authorized(self):
            expected = "127.0.0.1:" + str(self.server.server_port)
            if self.headers.get("Host") != expected:
                return False
            if self.headers.get("Origin") not in (None, "http://" + expected):
                return False
            supplied = self.headers.get("Authorization", "")
            return hmac.compare_digest(supplied, "Bearer " + studio.token)

        def do_GET(self):
            parsed = urllib.parse.urlsplit(self.path)
            static = {"/": ("studio.html", "text/html; charset=utf-8"), "/studio.css": ("studio.css", "text/css; charset=utf-8"), "/studio.js": ("studio.js", "text/javascript; charset=utf-8")}
            if parsed.path in static:
                filename, content_type = static[parsed.path]
                self.send(200, (WEB / filename).read_bytes(), content_type)
                return
            if not self.authorized():
                self.send(403, {"error": "Open the private studio URL printed by the CLI. Requests from other origins are refused."})
                return
            try:
                query = urllib.parse.parse_qs(parsed.query)
                if parsed.path == "/api/sessions":
                    self.send(200, {"sessions": [{key: value for key, value in item.items() if key != "path"} for item in studio.sessions],
                                    "resume_job": studio.resume_job, "resume_choices": studio.resume_choices, "durable_jobs": studio.durable_jobs})
                    return
                if parsed.path == "/api/jobs":
                    self.send(200, {"jobs": [{"id": item["id"], "stage": item["stage"], "status": item["status"], "updated_at": item.get("updated_at", "")}
                                             for item in sorted(studio.jobs.values(), key=lambda item: item.get("updated_at", ""), reverse=True)[:100]]})
                    return
                job = studio.job(query.get("job", [None])[0])
                if parsed.path == "/api/status":
                    with studio.action_lock, studio.lock:
                        result = copy.deepcopy({key: value for key, value in job.items() if key not in {"directory", "review_directory", "generation", "package", "plan"}})
                        result["publication_attempted"] = "package" in job and (job["package"] / "publication.json").exists()
                        if "delivery_result" not in result and job["stage"] == "generate" and job["status"] == "done":
                            result["delivery_result"] = copy.deepcopy(job.get("result"))
                        if result.get("delivery_result") and job["status"] != "running":
                            generation, snapshot = studio.delivery_snapshot(job)
                            result["delivery_result"]["snapshot"] = snapshot
                    self.send(200, result)
                elif parsed.path == "/api/review":
                    review, baseline = load_review(job.get("review_directory", job["directory"] / "review"))
                    self.send(200, present_review(review, baseline))
                elif parsed.path == "/api/file":
                    name = query.get("name", [""])[0]
                    with studio.action_lock, studio.lock:
                        generation, snapshot = studio.delivery_snapshot(job)
                        if self.headers.get("X-Delivery-Snapshot") != snapshot["id"]:
                            raise ValueError("Delivery snapshot changed or missing; reopen the completed output")
                        if name not in snapshot["files"]:
                            raise ValueError("This file was not selected for delivery")
                        target = generation / name if name == "deliverables.zip" else generation / "deliverables" / name
                        content = target.read_bytes()
                        if hashlib.sha256(content).hexdigest() != snapshot["files"][name]:
                            raise ValueError("Delivery changed while reading")
                    self.send(200, content, "application/zip" if name == "deliverables.zip" else "text/plain; charset=utf-8", snapshot["id"])
                else:
                    self.send(404, {"error": "Not found"})
            except (ValueError, OSError, KeyError) as error:
                self.send(400, {"error": str(error)[:1200]})

        def do_POST(self):
            if not self.authorized():
                self.send(403, {"error": "Request is not authorized for this local studio"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 2 * 1024 * 1024 or self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                    raise ValueError("Expected a bounded JSON request")
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise ValueError("Expected a JSON object")
                self.send(200, studio.action(urllib.parse.urlsplit(self.path).path, data))
            except (ValueError, OSError, KeyError, TypeError) as error:
                self.send(400, {"error": str(error)[:1200]})

    return Handler


def serve(home, output, session=None, port=0, open_generation=None, review_directory=None, **settings):
    studio = Studio(home, output, session, open_generation, review_directory, **settings)
    with ThreadingHTTPServer(("127.0.0.1", port), handler_for(studio)) as server:
        print(f"Private review studio: http://127.0.0.1:{server.server_port}/#access={studio.token}", flush=True)
        print("Local-only. No model calls or upload until explicitly selected. Stop this process to close the studio.", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
