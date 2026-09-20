import argparse
import hashlib
import json
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DIRECTORIES = {".plugin", ".codex-plugin", "docs", "scripts", "session_spec", "skills", "third_party"}
ROOT_FILES = {"plugin.json", "agency.json", "README.md", "pyproject.toml", "package.json", "package-lock.json"}


def build(destination, dependencies=False):
    destination = Path(destination).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise ValueError("Release file already exists; use a new filename")
    files = []
    roots = DIRECTORIES | ({"node_modules"} if dependencies else set())
    for path in sorted(ROOT.rglob("*")):
        relative = path.relative_to(ROOT)
        if not path.is_file() or "__pycache__" in relative.parts or path.suffix == ".pyc":
            continue
        if relative.parts[0] not in roots and relative.as_posix() not in ROOT_FILES:
            continue
        if path.is_symlink() or not path.resolve().is_relative_to(ROOT):
            raise ValueError("Refusing to bundle linked files outside the plugin")
        files.append(path)
    manifest = json.loads((ROOT / "plugin.json").read_bytes())
    marketplace = {"name": "learning-to-spec-local", "owner": {"name": "Local"}, "metadata": {"description": "Privacy-first session publishing", "version": manifest["version"]},
                   "plugins": [{"name": manifest["name"], "description": manifest["description"], "version": manifest["version"], "source": "./plugins/learning-to-spec"}]}
    hashes = {}
    with zipfile.ZipFile(destination, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(".github/plugin/marketplace.json", json.dumps(marketplace, indent=2))
        for path in files:
            name = "plugins/learning-to-spec/" + path.relative_to(ROOT).as_posix()
            content = path.read_bytes()
            hashes[name] = hashlib.sha256(content).hexdigest()
            archive.writestr(name, content)
        archive.writestr("release-manifest.json", json.dumps({"version": manifest["version"], "dependencies_bundled": dependencies, "files": hashes}, indent=2))
    return {"release": str(destination), "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(), "files": len(files), "dependencies_bundled": dependencies,
            "note": "Contains production code and a Copilot marketplace, not sessions, privacy reviews, outputs or experiment files."}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--with-dependencies", action="store_true")
    args = parser.parse_args()
    print(json.dumps(build(args.out, args.with_dependencies), indent=2))
