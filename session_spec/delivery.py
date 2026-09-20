import hashlib
import io
import json
import zipfile
from pathlib import Path

from .storage import file_hash, write_json


READERS = {"human", "agent", "both"}
DESTINATIONS = {"local", "artifactstore"}


def preferences(readers, destination, audience):
    if readers not in READERS or destination not in DESTINATIONS:
        raise ValueError("Explicitly choose Human, Agent or both, and local files or ArtifactStore")
    if destination == "local" and audience != "local":
        raise ValueError("Local delivery requires the local audience")
    if not isinstance(audience, str) or destination == "artifactstore" and audience != "root" and not audience.startswith("team:"):
        raise ValueError("ArtifactStore delivery requires an explicit root or team audience")
    return {"readers": readers, "destination": destination}


def filenames(readers, evidence=True, sharing=False):
    if readers not in READERS:
        raise ValueError("Unknown reader selection")
    names = []
    if readers in {"human", "both"}:
        names.append("index.html" if sharing else "human-spec.html")
    if readers in {"agent", "both"}:
        names.append("agent-spec.md")
        if evidence:
            names.append("evidence.md")
    return names


def deliver(generation, selection):
    generation = Path(generation).resolve()
    receipt = json.loads((generation / "reduced/reduction.json").read_bytes())
    selected = preferences(selection.get("readers"), selection.get("destination"), receipt["audience"])
    if receipt.get("preferences") != selected:
        raise ValueError("Reader/destination choice does not match the reviewed source")
    target = generation / "deliverables"
    if target.is_symlink():
        raise ValueError("Delivery directory cannot be a link")
    names = filenames(selected["readers"])
    if target.exists() and {path.name for path in target.iterdir()} - set(names):
        raise ValueError("Unexpected file in delivery directory; use a new generation")
    contents = {name: (generation / "story" / name).read_bytes() for name in names}
    target.mkdir(exist_ok=True)
    for name, content in contents.items():
        if (target / name).is_symlink():
            raise ValueError("Delivery files cannot be links")
        (target / name).write_bytes(content)
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for name, content in contents.items():
            bundle.writestr(name, content)
    archive_path = generation / "deliverables.zip"
    if archive_path.is_symlink():
        raise ValueError("Delivery archive cannot be a link")
    archive_path.write_bytes(archive.getvalue())
    manifest = {"schema": "reader-delivery/v1", "preferences": selected,
                "files": {name: hashlib.sha256(content).hexdigest() for name, content in contents.items()},
                "archive_sha256": file_hash(archive_path), "review_id": receipt["review_id"],
                "note": "Only selected reader files are delivered. The full working draft and private support remain in the generation workspace."}
    write_json(generation / "delivery.json", manifest)
    return {"output": str(target), "files": names, "bundle": str(archive_path), "preferences": selected}


def validate_delivery(generation, selection):
    generation = Path(generation).resolve()
    manifest = json.loads((generation / "delivery.json").read_bytes())
    expected = set(filenames(selection["readers"]))
    if manifest.get("schema") != "reader-delivery/v1" or manifest.get("preferences") != selection or set(manifest.get("files", {})) != expected:
        raise ValueError("Delivered files do not match the explicit reader selection")
    directory = generation / "deliverables"
    if directory.is_symlink() or {path.name for path in directory.iterdir()} != expected:
        raise ValueError("Delivery folder contains unexpected files or links")
    for name, expected_hash in manifest["files"].items():
        if (directory / name).is_symlink() or file_hash(directory / name) != expected_hash:
            raise ValueError("Delivered file changed after generation")
    if (generation / "deliverables.zip").is_symlink() or file_hash(generation / "deliverables.zip") != manifest["archive_sha256"]:
        raise ValueError("Delivery archive changed after generation")
    return manifest
