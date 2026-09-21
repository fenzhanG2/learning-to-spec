import json
import shutil
import tempfile
from pathlib import Path

from .agent_package import HUMAN_PRESENTATION, write_agent_package
from .locking import export_lock
from .pipeline import write_json
from .storage import file_hash
from .story_pipeline import render_story, validate_story


def refresh_story(directory, destination):
    directory = Path(directory).expanduser().resolve()
    destination = Path(destination).expanduser().resolve()
    if directory == destination or directory.is_relative_to(destination) or destination.is_relative_to(directory):
        raise ValueError("Presentation refresh requires a separate new output directory")
    support = directory / "_support"
    report_path = support / "story-report.json"
    previous_hash = file_hash(report_path)
    previous = json.loads(report_path.read_bytes())
    if previous.get("output_schema") not in {"story-output/v5", "story-output/v6", "story-output/v7", "story-output/v8", "story-output/v9"}:
        raise ValueError("Presentation refresh needs a reviewed v5/v6/v7/v8/v9 story; regenerate older stories with story")
    validation = validate_story(directory)
    if not validation["valid"]:
        raise ValueError("Cannot refresh an invalid story: " + "; ".join(validation["issues"]))
    source = json.loads((support / "source.json").read_bytes())
    original = Path(source["source_path"]).resolve().parent
    if destination == original or destination.is_relative_to(original):
        raise ValueError("Output cannot overwrite the historical session")
    with export_lock(destination):
        if any(path.name != ".export.lock" for path in destination.iterdir()):
            raise ValueError("Presentation refresh requires an empty output directory")
        with tempfile.TemporaryDirectory(prefix="story-refresh-", dir=destination.parent) as temporary:
            staged = Path(temporary)
            staged_support = staged / "_support"
            staged_support.mkdir()
            for filename, expected in previous["support_hashes"].items():
                target = staged_support / filename
                shutil.copy2(support / filename, target)
                if file_hash(target) != expected:
                    raise ValueError("Source story changed during presentation refresh")
            shutil.copy2(report_path, staged_support / "previous-story-report.json")
            if file_hash(staged_support / "previous-story-report.json") != previous_hash:
                raise ValueError("Source report changed during presentation refresh")
            if (support / "presentation.json").is_file():
                shutil.copy2(support / "presentation.json", staged_support / "presentation.json")
            write_json(staged_support / "story-source.json", source)
            article = json.loads((staged_support / "article.json").read_bytes())
            packet = json.loads((staged_support / "input.json").read_bytes())
            renderer = "companion/v8" if previous.get("evidence_renderer") == "companion/v8" else "companion/v7"
            agent_files = write_agent_package(article, packet, previous["language"], staged, staged_support,
                                             evidence_renderer=renderer if article.get("agent_detail", {}).get("schema") == "agent-detail/v3" else None)
            write_json(staged_support / "human-presentation.json", HUMAN_PRESENTATION)
            render_story(staged_support, staged / "human-spec.html")
            report = {**previous, "output_schema": "story-output/v6", "generation_method": "decision-handoff-render/v1",
                      "calls": [], "presentation_refresh": {"previous_report_sha256": previous_hash,
                          "edition_sha256": file_hash(staged_support / "edition.json"),
                          "semantic_review": "Inherited unchanged reviewed edition; no new model review or source-project execution."},
                      "hashes": {name: file_hash(staged / name) for name in ("human-spec.html", *agent_files)},
                      "support_hashes": {path.name: file_hash(path) for path in staged_support.iterdir() if path.is_file()}}
            if article.get("agent_detail", {}).get("schema") == "agent-detail/v3":
                report.update(output_schema="story-output/v9", generation_method="markdown-files-render/v1", evidence_renderer=renderer)
            write_json(staged_support / "story-report.json", report)
            validation = validate_story(staged)
            if not validation["valid"]:
                raise ValueError("Refreshed story failed validation: " + "; ".join(validation["issues"]))
            shutil.copytree(staged_support, destination / "_support")
            for filename in ("human-spec.html", *agent_files):
                shutil.copy2(staged / filename, destination / filename)
    return {"status": report["status"], "output": str(destination), "model_calls": 0,
            "note": "Presentation-only refresh; reviewed content and evidence are unchanged."}
