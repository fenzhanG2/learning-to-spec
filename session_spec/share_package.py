import io
import hashlib
import json
import zipfile
from pathlib import Path

from .reduction import digest, secure_baseline
from .reduction_rules import detect
from .storage import file_hash, write_json
from .story_pipeline import validate_story
from .delivery import filenames


ALLOWED = {"index.html", "agent-spec.md", "evidence.md"}


def prepare_package(story, destination, audience, include_evidence=True, readers="both"):
    story, destination = Path(story).resolve(), Path(destination).resolve()
    if story == destination or destination.is_relative_to(story) or story.is_relative_to(destination):
        raise ValueError("Share package must use a separate directory")
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("Share package already exists; use a new directory")
    validation = validate_story(story)
    if not validation["valid"]:
        raise ValueError("Story is not a validated publication")
    source = json.loads((story / "_support/source.json").read_bytes())
    reduced_path = Path(source["source_path"])
    receipt_path = reduced_path.parent / "reduction.json"
    if not receipt_path.is_file():
        raise ValueError("Sharing requires a story generated from a reviewed, reduced session. Old unfiltered stories cannot be uploaded directly.")
    receipt = json.loads(receipt_path.read_bytes())
    if receipt.get("schema") != "reduced-session/v1" or receipt.get("reduced_sha256") != source["source_sha256"] or file_hash(reduced_path) != source["source_sha256"]:
        raise ValueError("Privacy receipt does not match the story source")
    if receipt.get("audience") != audience or audience == "local":
        raise ValueError("Sharing audience differs from the reviewed audience; review for root or the selected team first")
    selection = receipt.get("preferences")
    if selection is not None and selection != {"readers": readers, "destination": "artifactstore"}:
        raise ValueError("Sharing requires the reviewed reader choice and an explicit ArtifactStore selection")
    files = {name: (story / ("human-spec.html" if name == "index.html" else name)).read_bytes()
             for name in filenames(readers, include_evidence, sharing=True)}
    findings = []
    for filename, content in files.items():
        text = content.decode("utf-8")
        if secure_baseline(text) != text:
            raise ValueError("Hard-sensitive content remains in " + filename + "; regenerate through the privacy pipeline")
        for start, end, category in detect(text):
            findings.append({"id": "F" + digest([filename, start, end, text[start:end]])[:16], "file": filename,
                             "category": category, "text": text[start:end], "start": start, "end": end})
    destination.mkdir(parents=True, exist_ok=True)
    payload = destination / "files"
    payload.mkdir()
    for filename, content in files.items():
        (payload / filename).write_bytes(content)
    manifest = {"schema": "share-package/v2", "readers": readers, "audience": audience, "privacy_review_id": receipt["review_id"],
                "files": {name: file_hash(payload / name) for name in sorted(files)},
                "evidence_included": include_evidence, "findings": findings,
                "limitations": ["Final review covers all allowlisted bytes, including evidence; automated detection can still miss contextual disclosure.",
                                "No raw sessions, reversal maps, review files, _support files or caches are included.",
                                "Without evidence.md, citation notes are unavailable to recipients." if not include_evidence else "Evidence is reduced but may still disclose task details; review it before sharing."]}
    manifest["package_id"] = digest(manifest)
    write_json(destination / "manifest.json", manifest)
    return manifest


def load_package(directory):
    directory = Path(directory).resolve()
    manifest = json.loads((directory / "manifest.json").read_bytes())
    identity = {key: value for key, value in manifest.items() if key != "package_id"}
    if manifest.get("schema") not in {"share-package/v1", "share-package/v2"} or digest(identity) != manifest.get("package_id"):
        raise ValueError("Package manifest changed; prepare again")
    files = manifest.get("files", {})
    readers = manifest.get("readers", "both")
    required = set(filenames(readers, evidence=False, sharing=True))
    allowed = set(filenames(readers, sharing=True))
    if not required <= set(files) <= allowed:
        raise ValueError("Package contains an unapproved filename")
    actual = {path.name for path in (directory / "files").iterdir()}
    if actual != set(files):
        raise ValueError("Unexpected content in the share directory")
    for filename, expected in files.items():
        path = directory / "files" / filename
        if path.is_symlink() or not path.is_file() or file_hash(path) != expected:
            raise ValueError("Package bytes changed; approval is invalid")
        if secure_baseline(path.read_text(encoding="utf-8")) != path.read_text(encoding="utf-8"):
            raise ValueError("Hard-sensitive content blocks publication")
    return manifest


def approve_package(directory, package_id, acknowledged_findings, reviewed_all_files=False, *, confirmed_publish=False):
    directory = Path(directory).resolve()
    manifest = load_package(directory)
    if package_id != manifest["package_id"] or not (reviewed_all_files is True or confirmed_publish is True):
        raise ValueError("Explicit approval of the current selected files is required")
    if set(acknowledged_findings) != {finding["id"] for finding in manifest["findings"]}:
        raise ValueError("Every final disclosure finding needs explicit acknowledgement; edit the privacy choices and regenerate to remove it")
    approval = {"schema": "share-approval/v1", "package_id": package_id, "audience": manifest["audience"],
                "reviewed_all_files": reviewed_all_files is True, "confirmed_publish": confirmed_publish is True,
                "acknowledged_findings": sorted(acknowledged_findings)}
    write_json(directory / "approval.json", approval)
    return approval


def package_bytes(directory):
    directory = Path(directory).resolve()
    manifest = load_package(directory)
    approval = json.loads((directory / "approval.json").read_bytes())
    if approval.get("package_id") != manifest["package_id"] or approval.get("audience") != manifest["audience"] or not (approval.get("reviewed_all_files") is True or approval.get("confirmed_publish") is True) or set(approval.get("acknowledged_findings", [])) != {finding["id"] for finding in manifest["findings"]}:
        raise ValueError("Current package lacks final privacy approval")
    return _archive(directory, manifest)


def package_preview(directory):
    directory = Path(directory).resolve()
    manifest = load_package(directory)
    return _archive(directory, manifest)


def _archive(directory, manifest):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for filename in sorted(manifest["files"]):
            info = zipfile.ZipInfo(filename, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            content = (directory / "files" / filename).read_bytes()
            if hashlib.sha256(content).hexdigest() != manifest["files"][filename]:
                raise ValueError("Package bytes changed while bundling; approval is invalid")
            archive.writestr(info, content)
    return manifest, stream.getvalue()
