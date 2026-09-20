import argparse
import json
import sys
from pathlib import Path

from .ingest import copilot_home, list_sessions
from .pipeline import prepare, run_export
from .privacy import sanitize
from .render import lines_for_spec
from .validation import validate_spec
from .story_pipeline import run_story, validate_story
from .story_refresh import refresh_story
from . import private_cli


def local_defaults():
    path = Path.home() / ".learning-to-spec" / "config.json"
    if not path.is_file():
        path = Path.home() / ".copilot-session-spec" / "config.json"
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("Expected an object")
        return value
    except (ValueError, OSError):
        raise ValueError("Invalid local learning-to-spec configuration: " + str(path)) from None


def parser():
    command = argparse.ArgumentParser(description="Export observable Copilot session trajectories into human and agent work specs.")
    command.add_argument("--home", type=Path, default=copilot_home(), help="Copilot data directory (read-only)")
    subcommands = command.add_subparsers(dest="command", required=True)
    private_cli.configure(subcommands)
    listing = subcommands.add_parser("list", help="List local Copilot sessions without sending content to a model")
    listing.add_argument("--limit", type=int, default=30)
    listing.add_argument("--query", default="")
    export = subcommands.add_parser("export", help="Generate two specs using the authenticated Copilot CLI")
    export.add_argument("session", help="UUID, unique UUID prefix, directory or events.jsonl path")
    export.add_argument("--out", type=Path)
    export.add_argument("--language", default="auto", help="Naturally follow source-session language; specify a tag only to request a translation")
    export.add_argument("--chunk-chars", type=int, default=80000)
    export.add_argument("--max-calls", type=int, default=40)
    export.add_argument("--timeout", type=int, default=600)
    export.add_argument("--workers", type=int, choices=range(1, 5), default=2, help="Concurrent chunk calls; synthesis/review remain ordered")
    export.add_argument("--model")
    export.add_argument("--gh-host", help="Use this existing gh login; token stays in child-process environment only")
    export.add_argument("--copilot", help="Copilot executable override")
    export.add_argument("--dry-run", action="store_true", help="Show input coverage/call estimate; no model calls or output writes")
    export.add_argument("--resume", action="store_true", help="Reuse successful cached calls if source snapshot matches")
    export.add_argument("--rebuild", action="store_true", help="With --resume, rebuild from original extraction caches instead of the latest candidate")
    export.add_argument("--no-review", action="store_true", help="Mark outputs unreviewed; skip semantic reviewer")
    story = subcommands.add_parser("story", help="Generate the human HTML story and detailed Agent Markdown through the native pipeline")
    story.add_argument("session", nargs="?")
    story.add_argument("--from-export", type=Path, help="Reuse an existing canonical export as input, not a hand-edited report")
    story.add_argument("--revise-from", type=Path, help="Use a source-matched prior edition/candidate as starting material; review it again under the current contract")
    story.add_argument("--editorial-feedback", type=Path, help="Source-bound JSON review findings; verify and repair through the pipeline, not manual report edits")
    story.add_argument("--out", type=Path, required=True)
    story.add_argument("--model")
    story.add_argument("--language", default="auto", help="Naturally follow source-session language; no target language is prescribed by default")
    story.add_argument("--gh-host")
    story.add_argument("--max-calls", type=int, default=40)
    story.add_argument("--timeout", type=int, default=900)
    story.add_argument("--resume", action="store_true")
    story.add_argument("--dry-run", action="store_true")
    story_validation = subcommands.add_parser("validate-story", help="Verify accepted HTML/Markdown and their source model offline")
    story_validation.add_argument("directory", type=Path)
    story_refresh = subcommands.add_parser("refresh-story", help="Re-render a reviewed story with the current presentation; no model calls or new factual review")
    story_refresh.add_argument("directory", type=Path)
    story_refresh.add_argument("--out", type=Path, required=True)
    validation = subcommands.add_parser("validate", help="Check evidence references, coverage, attribution and both projections offline")
    validation.add_argument("directory", type=Path)
    expand = subcommands.add_parser("expand", help="Read selected sanitized evidence; never executes historical commands")
    expand.add_argument("directory", type=Path)
    expand.add_argument("refs", nargs="+")
    return command


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    try:
        arguments = parser().parse_args(argv)
        if hasattr(arguments, "chunk_chars") and not 10000 <= arguments.chunk_chars <= 100000:
            raise ValueError("--chunk-chars must be between 10000 and 100000.")
        if arguments.command in private_cli.COMMANDS:
            result = private_cli.run(arguments, local_defaults())
        elif arguments.command == "list":
            result = list_sessions(arguments.home, arguments.limit, arguments.query)
        elif arguments.command == "story":
            if bool(arguments.session) == bool(arguments.from_export):
                raise ValueError("Select exactly one session or --from-export")
            if arguments.max_calls < 0:
                raise ValueError("--max-calls cannot be negative; zero allows only matching cached stages")
            if arguments.dry_run:
                result = {"stages": ["source snapshot", "canonical work model", "source language", "joint human story and detailed Agent working record", "fresh delivered-pair transfer probe", "whole-document review and bounded repairs", "complete tool ledger and HTML/Markdown render"],
                          "max_calls": arguments.max_calls, "additional_story_calls_minimum": 2 if arguments.revise_from else 3,
                          "additional_story_calls_with_repairs_maximum": arguments.max_calls, "matching_cache_calls_minimum": 0,
                          "language": arguments.language, "format_retry_policy": "At most one additional call per invalid JSON response, within the total budget",
                          "editorial_feedback": bool(arguments.editorial_feedback),
                          "reference_project_dependencies": [], "historical_commands_executed": False,
                          "note": "The minimum assumes an uncached valid draft and no corrections (revision omits drafting); a matching cache can use zero calls. The maximum is the shared --max-calls budget, not an extra story allowance; canonical extraction and all JSON/citation retries consume it too. A budget below the uncached minimum requires matching cached stages. Full story packet limit: 800,000 characters; transfer-probe pair limit: 240,000 characters."}
            else:
                defaults = local_defaults()
                result = run_story(arguments.session, arguments.home, arguments.out, from_export=arguments.from_export,
                                   model=arguments.model or defaults.get("model"), gh_host=arguments.gh_host or defaults.get("gh_host"),
                                   resume=arguments.resume, max_calls=arguments.max_calls, timeout=arguments.timeout,
                                   editorial_feedback=arguments.editorial_feedback, language=arguments.language, revise_from=arguments.revise_from)
        elif arguments.command == "refresh-story":
            result = refresh_story(arguments.directory, arguments.out)
        elif arguments.command == "validate-story":
            result = validate_story(arguments.directory)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["valid"] else 1
        elif arguments.command == "export":
            if arguments.rebuild and not arguments.resume:
                raise ValueError("--rebuild requires --resume.")
            if arguments.dry_run:
                metadata, records, chunks, coverage = prepare(arguments.session, arguments.home, arguments.chunk_chars)
                result = {"source": metadata, "chunks": len(chunks), "estimated_min_model_calls": len(chunks) + 1 + 2 * (not arguments.no_review), "possible_additional_content_repair_calls": 6 if not arguments.no_review else 2, "format_retry_note": "JSON/schema retries also count against --max-calls.", "root_request_count": len(coverage["root_request_refs"]), "human_feedback_count": len(coverage["human_feedback_refs"]), "coverage_policy": coverage["policy"], "model_input_chars": sum(len(json.dumps(chunk, ensure_ascii=False)) for chunk in chunks)}
            else:
                defaults = local_defaults()
                destination = arguments.out or (Path.home() / ".learning-to-spec" / "exports" / Path(arguments.session).stem)
                result = run_export(
                    arguments.session, arguments.home, destination,
                    language=arguments.language, chunk_chars=arguments.chunk_chars,
                    review=not arguments.no_review, resume=arguments.resume, rebuild=arguments.rebuild,
                    max_calls=arguments.max_calls, timeout=arguments.timeout,
                    model=arguments.model or defaults.get("model"),
                    gh_host=arguments.gh_host or defaults.get("gh_host"), executable=arguments.copilot,
                    workers=arguments.workers,
                )
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return 2 if result["status"] == "needs_review" else 0
        else:
            directory = arguments.directory.resolve()
            records = [json.loads(line) for line in (directory / "evidence.jsonl").read_text(encoding="utf-8").splitlines() if line]
            if arguments.command == "expand":
                wanted = set(arguments.refs)
                result = [record for record in records if record["ref"] in wanted]
                if len(result) != len(wanted):
                    raise ValueError("One or more evidence refs do not exist.")
            else:
                wrapper = json.loads((directory / "spec.json").read_text(encoding="utf-8"))
                report = json.loads((directory / "report.json").read_text(encoding="utf-8"))
                issues = validate_spec(wrapper["spec"], records)
                for filename, agent in (("human-spec.md", False), ("agent-spec.md", True)):
                    expected = lines_for_spec(wrapper["spec"], wrapper["source"], report, agent)
                    path = directory / filename
                    if not path.exists() or path.read_text(encoding="utf-8") != expected:
                        issues.append({"severity": "error", "section": filename, "message": "Projection differs from canonical spec/report."})
                result = {"valid": not issues, "status": report["status"], "issues": issues, "note": "Mechanical consistency is not proof of semantic correctness."}
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return 1 if issues else 0
        print(json.dumps(sanitize(result), ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, KeyError, TypeError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
