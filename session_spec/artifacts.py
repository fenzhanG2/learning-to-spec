import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from pathlib import Path

from .reduction import digest
from .share_package import package_bytes
from .storage import write_json


BASE = "https://artifacts.turing.azure.com"
RESOURCE = "api://bbc8c724-3dce-4ad9-a39c-9f3164b052d5"
PRIVATE = {"allAuthenticated": False, "owners": [], "users": [], "groups": [], "aadGroups": []}


def verify_file(content, expected, filename, site):
    response_hash = hashlib.sha256(content).hexdigest()
    method = "exact_bytes"
    normalized = content
    if response_hash != expected and filename == "index.html":
        head = (b'<script src="/url-utils.js"></script><script src="/link-security.js"></script>'
                b'<script data-as-csrf src="/csrf-bootstrap.js"></script>')
        normalized = normalized.replace(b"<head>" + head, b"<head>", 1)
        normalized = re.sub(
            rb'\n<link data-artifactstore-favicon rel="icon" type="image/svg\+xml" href="/favicon.svg\?v=[0-9.]+">'
            rb'\n<link data-artifactstore-favicon rel="apple-touch-icon" href="/apple-touch-icon.png\?v=[0-9.]+">\n</head>',
            b"</head>", normalized, count=1)
        widget = (f'<script data-as-editor src="/_editor/widget.js" data-site="{site}" '
                  f'data-page="{filename}"></script>').encode("utf-8")
        normalized = normalized.replace(widget + b"</body>", b"</body>", 1)
        method = "authored_html_with_known_service_decorations"
    if hashlib.sha256(normalized).hexdigest() != expected:
        raise ValueError("Uploaded file differs from approved bytes: " + filename)
    return {"method": method, "approved_sha256": expected, "response_sha256": response_hash}


def verify_policy(plan, client, details):
    if plan["team"]:
        policy = client.request("GET", "/api/teams/" + urllib.parse.quote(plan["team"], safe=""))
        if details.get("team") != plan["team"] or policy.get("team", policy).get("sharing") != plan["viewer_policy"]:
            raise ValueError("Team access does not match the approved policy; inspect the artifact")
    elif not isinstance(details.get("sharing"), dict) or details["sharing"].get("allAuthenticated") is not False or any(details["sharing"].get(key) for key in PRIVATE if key != "allAuthenticated"):
        raise ValueError("Owner-only access could not be verified; inspect the artifact")


def verify_publication(directory, plan, client):
    directory = Path(directory).resolve()
    manifest, archive = package_bytes(directory)
    identity = {key: value for key, value in plan.items() if key != "plan_id"}
    if (plan.get("schema") != "artifact-plan/v1" or plan.get("destination") != BASE
            or digest(identity) != plan.get("plan_id") or plan.get("package_id") != manifest["package_id"]
            or plan.get("zip_sha256") != hashlib.sha256(archive).hexdigest()
            or plan.get("audience") != manifest["audience"] or plan.get("files") != sorted(manifest["files"])
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", plan.get("site", ""))):
        raise ValueError("Verification requires the unchanged approved package and destination plan")
    receipt = json.loads((directory / "publication.json").read_bytes())
    if receipt.get("plan_id") != plan["plan_id"] or receipt.get("site") != plan["site"] or receipt.get("package_id") != manifest["package_id"]:
        raise ValueError("Publication receipt does not match the approved plan")
    details = client.request("GET", "/api/sites/" + plan["site"])
    verify_policy(plan, client, details)
    if set(details.get("files", [])) != set(manifest["files"]):
        raise ValueError("Remote file list differs from the approved package")
    checks = {}
    for filename, expected in manifest["files"].items():
        content = client.request("GET", "/sites/" + plan["site"] + "/" + filename + "?raw=1", raw=True)
        checks[filename] = verify_file(content, expected, filename, plan["site"])
    after = client.request("GET", "/api/sites/" + plan["site"])
    verify_policy(plan, client, after)
    if set(after.get("files", [])) != set(manifest["files"]):
        raise ValueError("Remote file list changed during verification")
    receipt.update(status="verified", verification=checks,
                   files={name: BASE + "/sites/" + plan["site"] + "/" + name + ("?raw=1" if name.endswith(".md") else "") for name in manifest["files"]})
    receipt["note"] = "Readback verified the approved authored content. ArtifactStore can decorate HTML with its own security, favicon and editor markup; this is not a raw storage-blob attestation. No remote writes occur during verification."
    write_json(directory / "publication.json", receipt)
    return receipt


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, stream, code, message, headers, new_url):
        raise ValueError("ArtifactStore redirected a credential-bearing request; sign in again through the normal Azure CLI flow")


class ArtifactError(ValueError):
    def __init__(self, status):
        self.status = status
        super().__init__(f"ArtifactStore returned HTTP {status}; no automatic write retry was performed")


class ArtifactClient:
    def __init__(self, token=None):
        if token is None:
            executable = shutil.which("az")
            if not executable:
                raise ValueError("Azure CLI is required for upload. Install it and authenticate to your Microsoft tenant; do not paste tokens into the plugin.")
            result = subprocess.run([executable, "account", "get-access-token", "--resource", RESOURCE, "--query", "accessToken", "-o", "tsv"],
                                    capture_output=True, text=True, encoding="utf-8", timeout=60,
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            if result.returncode or not result.stdout.strip():
                raise ValueError("Azure CLI could not acquire an ArtifactStore token. Complete normal Azure sign-in/consent, then retry. Credentials and provider output are not logged.")
            token = result.stdout.strip()
        self.token = token
        self.opener = urllib.request.build_opener(NoRedirect())

    def request(self, method, path, data=None, content_type="application/json", raw=False):
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("Only same-service ArtifactStore paths are supported")
        body = json.dumps(data).encode() if data is not None and content_type == "application/json" else data
        request = urllib.request.Request(BASE + path, data=body, method=method,
                                         headers={"Authorization": "Bearer " + self.token, "Content-Type": content_type})
        try:
            with self.opener.open(request, timeout=90) as response:
                content = response.read(55 * 1024 * 1024)
                return content if raw else json.loads(content)
        except urllib.error.HTTPError as error:
            raise ArtifactError(error.code) from None
        except (urllib.error.URLError, TimeoutError):
            raise ValueError("ArtifactStore request did not complete. The remote state may have changed; inspect before retrying an upload.") from None

    def teams(self):
        return self.request("GET", "/api/teams")

    def upload(self, site, archive, team=None):
        boundary = "session-spec-" + uuid.uuid4().hex
        parts = []
        if team:
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="team"\r\n\r\n{team}\r\n'.encode())
        parts.extend([f'--{boundary}\r\nContent-Disposition: form-data; name="zip"; filename="session-spec.zip"\r\nContent-Type: application/zip\r\n\r\n'.encode(),
                      archive, f'\r\n--{boundary}--\r\n'.encode()])
        return self.request("POST", f"/api/sites/{site}/upload", b"".join(parts), "multipart/form-data; boundary=" + boundary)


def destination_plan(directory, site, client):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", site):
        raise ValueError("Site name must start with a letter/number and contain only letters, numbers, underscores or hyphens")
    manifest, archive = package_bytes(directory)
    audience = manifest["audience"]
    team = audience[5:] if audience.startswith("team:") else None
    try:
        client.request("GET", "/api/sites/" + site)
    except ArtifactError as error:
        if error.status != 404:
            raise
    else:
        raise ValueError("An artifact with this name already exists. Automatic overwrite is disabled; choose a new name.")
    policy = PRIVATE
    if team:
        details = client.request("GET", "/api/teams/" + urllib.parse.quote(team, safe=""))
        details = details.get("team", details) if isinstance(details, dict) else {}
        if not isinstance(details, dict) or not isinstance(details.get("sharing"), dict):
            raise ValueError("Could not establish the selected team's viewer policy")
        policy = details["sharing"]
    plan = {"schema": "artifact-plan/v1", "site": site, "team": team, "audience": audience,
            "package_id": manifest["package_id"], "zip_sha256": hashlib.sha256(archive).hexdigest(),
            "viewer_policy": policy, "destination": BASE, "files": sorted(manifest["files"]),
            "note": "Root upload is owner-only plus service administrators. Team upload inherits the displayed team policy; permissions may change after publishing."}
    plan["plan_id"] = digest(plan)
    return plan


def publish_package(directory, plan, confirmation, client):
    directory = Path(directory).resolve()
    if confirmation != plan.get("plan_id"):
        raise ValueError("Explicit confirmation of the destination plan is required")
    current = destination_plan(directory, plan["site"], client)
    if current != plan:
        raise ValueError("Destination policy or package changed; review a fresh upload plan")
    manifest, archive = package_bytes(directory)
    receipt = {"schema": "artifact-publication/v1", "plan_id": plan["plan_id"], "site": plan["site"], "status": "starting",
               "package_id": manifest["package_id"], "url": BASE + "/sites/" + plan["site"] + "/"}
    write_json(directory / "publication.json", receipt)
    placeholder = io.BytesIO()
    with zipfile.ZipFile(placeholder, "w") as staging:
        if "index.html" in manifest["files"]:
            staging.writestr("index.html", "<!doctype html><title>Pending privacy-checked publication</title><p>No session content has been uploaded.</p>")
        else:
            staging.writestr("agent-spec.md", "# Pending privacy-checked publication\nNo session content has been uploaded.\n")
    try:
        client.upload(plan["site"], placeholder.getvalue(), plan["team"])
        receipt["status"] = "placeholder_created"
        write_json(directory / "publication.json", receipt)
        if not plan["team"]:
            client.request("PUT", "/api/sites/" + plan["site"] + "/sharing", PRIVATE)
        details = client.request("GET", "/api/sites/" + plan["site"])
        if plan["team"]:
            if details.get("team") != plan["team"]:
                raise ValueError("Placeholder team does not match the approved destination")
            team_details = client.request("GET", "/api/teams/" + plan["team"])
            team_details = team_details.get("team", team_details)
            if team_details.get("sharing") != plan["viewer_policy"]:
                raise ValueError("Team policy changed before content upload")
        elif not isinstance(details.get("sharing"), dict) or details["sharing"].get("allAuthenticated") is not False or any(details["sharing"].get(key) for key in PRIVATE if key != "allAuthenticated"):
            raise ValueError("Owner-only sharing could not be verified; session content was not uploaded")
        client.upload(plan["site"], archive, plan["team"])
        receipt["status"] = "uploaded_unverified"
        write_json(directory / "publication.json", receipt)
        return verify_publication(directory, plan, client)
    except (ValueError, OSError):
        receipt["note"] = "Stopped without an automatic retry or deletion. An empty placeholder or uploaded content may remain; inspect this exact artifact before recovery."
        write_json(directory / "publication.json", receipt)
        raise
