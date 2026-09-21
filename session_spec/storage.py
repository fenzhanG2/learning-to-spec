import hashlib
import json
import stat
from pathlib import Path


PROMPTS = Path(__file__).resolve().parent / "templates"


def unlinked_path(value):
    path = Path(value).absolute()
    for part in (path, *path.parents):
        try:
            metadata = part.lstat()
        except FileNotFoundError:
            continue
        attributes = getattr(metadata, "st_file_attributes", 0)
        tag = getattr(metadata, "st_reparse_tag", 0)
        if stat.S_ISLNK(metadata.st_mode) or (attributes & 0x400 and (not tag or tag & 0x20000000)):
            raise ValueError("Linked abstraction or review paths are not supported")
    return path.resolve()


def digest(content):
    return hashlib.sha256(content).hexdigest()


def file_hash(path):
    fingerprint = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            fingerprint.update(chunk)
    return fingerprint.hexdigest()


def write_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
