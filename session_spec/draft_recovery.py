import html
import json
import re
import zipfile

from .storage import digest, file_hash, unlinked_path, write_json


SCHEMA = "private-unvalidated-draft/v1"
MAX_BYTES = 2 * 1024 * 1024
FILES = {"human-spec.html", "agent-spec.md"}
WARNING = ("UNVALIDATED PRIVATE DRAFT — validation did not pass. Facts, citations and completeness may be wrong. "
           "Privacy review/redaction is NOT complete; sensitive content may remain, including credentials and personal details. "
           "Sharing requires an explicit risk override; uploading does not validate this draft. Check the original session before acting on it.")
CAUSES = {
    "draft_references_invalid": "Source citations were missing or invalid and one automatic repair did not produce a valid draft.",
    "draft_structure_invalid": "The draft format remained invalid after one automatic repair.",
    "draft_quality_invalid": "Source-quality review found a factual/material defect or could not return a valid assessment. This is not an approved final spec.",
    "draft_privacy_invalid": "Privacy review did not complete within its bounded attempt, or findings could not be matched literally to the draft. Redaction is incomplete; this is not a privacy-approved spec.",
}


def bounded_read(path):
    with unlinked_path(path).open("rb") as stream:
        content = stream.read(MAX_BYTES + 1)
    if len(content) > MAX_BYTES:
        raise ValueError("Recovery input exceeds the bounded local-draft size")
    return content


def readable(value, include_refs=False, depth=0):
    if depth > 16:
        return "[Deeply nested content omitted from this draft view; original diagnostics remain local.]"
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return "\n\n".join(key.replace("_", " ").title() + ":\n" + readable(item, include_refs, depth + 1)
                           for key, item in value.items() if isinstance(key, str)
                           and key not in {"human_input_coverage", "reader_coverage"}
                           and (include_refs or key not in {"refs", "entry_ref", "human_refs", "tool_refs"}))
    if isinstance(value, list):
        return "\n\n".join(readable(item, include_refs, depth + 1) for item in value)
    return json.dumps(value, ensure_ascii=False)


def fenced(value):
    fence = "`" * max(3, 1 + max((len(match) for match in re.findall(r"`+", value)), default=0))
    return fence + "text\n" + value + "\n" + fence


def create_recovery(job_directory, readers, cause):
    if readers not in {"human", "agent", "both"} or cause not in CAUSES:
        raise ValueError("Recovery requires explicit selected readers and a recognized draft failure")
    job_directory = unlinked_path(job_directory)
    support = unlinked_path(job_directory / "abstraction/story/_support")
    candidate = None
    source_name = None
    for name in ("fast-candidate-1.json", "fast-candidate-0.json"):
        path = unlinked_path(support / name)
        if path.is_file():
            value = json.loads(bounded_read(path))
            if isinstance(value, dict) and isinstance(value.get("article"), dict):
                candidate, source_name = value, name
                break
    if candidate is None:
        for name in ("fast-invalid-response-0.json", "fast-response-0.json"):
            path = unlinked_path(support / name)
            if path.is_file():
                raw = bounded_read(path).decode("utf-8")
                if name.startswith("fast-invalid"):
                    value = json.loads(raw)
                    raw = value.get("invalid_response", raw)
                candidate, source_name = {"article": {"opening": raw}}, name
                break
    if candidate is None:
        raise ValueError("No generated draft is available to recover")
    article = candidate["article"]
    issues = CAUSES[cause]
    quality = unlinked_path(support / "fast-quality.json")
    if cause == "draft_quality_invalid" and quality.is_file():
        issues += "\n\nRecorded review concerns (unredacted, local only):\n" + readable(json.loads(bounded_read(quality)).get("result", {}).get("issues", []), True)
    human = []
    if candidate.get("brief"):
        human.append(("Background and purpose", readable(candidate["brief"])))
    for key in ("title", "subtitle", "opening", "outcome", "route", "chapters", "checks"):
        if article.get(key):
            human.append((key.replace("_", " ").title(), readable(article[key])))
    if candidate.get("insights"):
        human.append(("Architecture and lessons", readable(candidate["insights"])))
    agent = readable(article.get("agent_markdown", ""), True)
    if article.get("agent_detail"):
        agent += "\n\n" + readable(article["agent_detail"], True)
    if not agent.strip():
        agent = "No complete Agent handoff was generated. Recoverable draft content follows:\n\n" + readable(candidate, True)
    directory = unlinked_path(job_directory / "unvalidated-draft")
    directory.mkdir(mode=0o700)
    output = unlinked_path(directory / "deliverables")
    output.mkdir(mode=0o700)
    if readers in {"human", "both"}:
        body = "".join("<section><h2>" + html.escape(title) + "</h2><pre>" + html.escape(text) + "</pre></section>" for title, text in human)
        page = ('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
                '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; base-uri \'none\'; form-action \'none\'">'
                '<title>Unvalidated private draft</title><style>body{max-width:900px;margin:48px auto;padding:0 24px;font:17px/1.7 system-ui;color:#24322d;background:#f7f8f5}'
                'aside{padding:20px;border:2px solid #ad6300;background:#fff1d8;border-radius:12px}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit}'
                'section{margin:28px 0;padding:20px;background:white;border-radius:12px}</style></head><body><h1>Unvalidated private draft</h1><aside>'
                + html.escape(WARNING) + '</aside><h2>What needs attention</h2><pre>' + html.escape(issues) + '</pre>' + body + '</body></html>')
        (output / "human-spec.html").write_text(page, encoding="utf-8", newline="\n")
    if readers in {"agent", "both"}:
        markdown = "# Unvalidated private draft\n\n" + WARNING + "\n\n## What needs attention\n\n" + fenced(issues)
        markdown += "\n\n## Recoverable handoff\n\n" + fenced(agent)
        markdown += "\n\nCitations are not validated here; consult the original session. No evidence companion or privacy-approved package was produced.\n"
        (output / "agent-spec.md").write_text(markdown, encoding="utf-8", newline="\n")
    names = sorted(path.name for path in output.iterdir())
    with zipfile.ZipFile(directory / "deliverables.zip", "x", zipfile.ZIP_DEFLATED) as archive:
        for name in names:
            archive.write(output / name, name)
    record = {"schema": SCHEMA, "cause": cause, "readers": readers, "privacy": "not_completed", "upload_allowed": False,
              "source_file": source_name, "source_sha256": file_hash(support / source_name),
              "files": {name: file_hash(output / name) for name in names}, "zip_sha256": file_hash(directory / "deliverables.zip")}
    write_json(directory / "manifest.json", record)
    return file_hash(directory / "manifest.json")


def recovery_snapshot(job_directory, expected):
    directory = unlinked_path(unlinked_path(job_directory) / "unvalidated-draft")
    manifest = bounded_read(directory / "manifest.json")
    if digest(manifest) != expected:
        raise ValueError("Unvalidated draft receipt changed")
    record = json.loads(manifest)
    selected = record.get("files")
    if (record.get("schema") != SCHEMA or record.get("upload_allowed") is not False or record.get("privacy") != "not_completed"
            or not isinstance(selected, dict) or not selected or not set(selected) <= FILES):
        raise ValueError("Invalid private draft receipt")
    files = {**selected, "deliverables.zip": record["zip_sha256"]}
    for name, fingerprint in files.items():
        path = directory / name if name == "deliverables.zip" else directory / "deliverables" / name
        if digest(bounded_read(path)) != fingerprint:
            raise ValueError("Private draft changed after recovery")
    return directory, {"id": expected, "files": files}


def prepare_recovery_package(job_directory, expected, destination, audience, accept_unvalidated=False):
    from .reduction import digest as identity_digest
    from .share_package import UNVALIDATED_SCHEMA

    if accept_unvalidated is not True:
        raise ValueError("Explicit acceptance of unvalidated content and incomplete privacy is required")
    if audience != "root" and (not isinstance(audience, str) or not re.fullmatch(r"team:[A-Za-z0-9_-]+", audience)):
        raise ValueError("Choose the ArtifactStore audience explicitly")
    directory, snapshot = recovery_snapshot(job_directory, expected)
    record = json.loads(bounded_read(directory / "manifest.json"))
    destination = unlinked_path(destination)
    destination.mkdir(mode=0o700)
    payload = destination / "files"
    payload.mkdir(mode=0o700)
    files = {}
    for name, fingerprint in snapshot["files"].items():
        if name == "deliverables.zip":
            continue
        content = bounded_read(directory / "deliverables" / name)
        if digest(content) != fingerprint:
            raise ValueError("Recovered draft changed while preparing upload")
        shared_name = "index.html" if name == "human-spec.html" else name
        (payload / shared_name).write_bytes(content)
        files[shared_name] = fingerprint
    manifest = {"schema": UNVALIDATED_SCHEMA, "readers": record["readers"], "audience": audience,
                "recovery_id": expected, "failure_code": record["cause"], "quality": "unvalidated", "privacy": "incomplete",
                "files": files, "evidence_included": False, "findings": [],
                "limitations": [WARNING, "Only the selected recovered reader files are included, not the raw session or diagnostic files."]}
    manifest["package_id"] = identity_digest(manifest)
    write_json(destination / "manifest.json", manifest)
    return manifest
