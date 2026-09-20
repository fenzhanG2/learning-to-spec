import hashlib
import json
from pathlib import Path


PROMPTS = Path(__file__).resolve().parent / "templates"


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
