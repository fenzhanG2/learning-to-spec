import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from session_spec.render import lines_for_spec
from session_spec.validation import validate_spec


def audit(directory):
    wrapper = json.loads((directory / "spec.json").read_text(encoding="utf-8"))
    report = json.loads((directory / "report.json").read_text(encoding="utf-8"))
    records = [json.loads(line) for line in (directory / "evidence.jsonl").read_text(encoding="utf-8").splitlines() if line]
    issues = validate_spec(wrapper["spec"], records)
    projections = {}
    for filename, agent in (("human-spec.md", False), ("agent-spec.md", True)):
        projections[filename] = (directory / filename).read_text(encoding="utf-8") == lines_for_spec(wrapper["spec"], wrapper["source"], report, agent)
    source = Path(wrapper["source"]["source_path"])
    digest = hashlib.sha256()
    with source.open("rb") as stream:
        for block in iter(lambda: stream.read(1048576), b""):
            digest.update(block)
    reports = [report]
    for path in (directory / ".attempts").glob("*.json"):
        reports.append(json.loads(path.read_text(encoding="utf-8")))
    receipts = {json.dumps(receipt, sort_keys=True): receipt for attempt in reports for receipt in attempt.get("calls", [])}
    return {
        "case": directory.name,
        "source_unchanged": digest.hexdigest() == wrapper["source"]["source_sha256"],
        "root_user_turns": wrapper["source"]["root_user_turns"],
        "interactive_answers": wrapper["source"].get("human_feedback_count", 0),
        "automated_control_answers": wrapper["source"].get("control_feedback_count", 0),
        "inputs_accounted_for": len(wrapper["spec"]["request_coverage"]),
        "trajectory_episodes": len(wrapper["spec"]["trajectory"]),
        "mechanical_issues": issues, "projections_consistent": projections,
        "review_status": report["status"],
        "semantic_errors": sum(entry["severity"] == "error" for entry in report.get("review", {}).get("issues", [])),
        "semantic_warnings": sum(entry["severity"] == "warning" for entry in report.get("review", {}).get("issues", [])),
        "review_stages": report.get("review", {}).get("stages", ["evidence"]),
        "model_tool_calls": sum(entry["tool_calls"] for entry in receipts.values() if "tool_calls" in entry),
        "recorded_generation_receipts": len(receipts),
        "receipts_without_tool_call_count": sum("tool_calls" not in entry for entry in receipts.values()),
        "human_spec": str(directory / "human-spec.md"), "agent_spec": str(directory / "agent-spec.md"),
    }


def main():
    parser = argparse.ArgumentParser(description="Audit already-exported real sessions without invoking a model.")
    parser.add_argument("directories", nargs="+", type=Path)
    parser.add_argument("--out", type=Path)
    arguments = parser.parse_args()
    results = [audit(directory.resolve()) for directory in arguments.directories]
    output = json.dumps({"cases": results, "scope": "Mechanical and source-integrity checks only; not a semantic quality score. Receipts include retained reports, not an exhaustive cost ledger of interrupted development runs."}, ensure_ascii=False, indent=2)
    if arguments.out:
        arguments.out.write_text(output + "\n", encoding="utf-8")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(output)
    return int(any(not result["source_unchanged"] or result["mechanical_issues"] or not all(result["projections_consistent"].values()) or result["model_tool_calls"] for result in results))


if __name__ == "__main__":
    raise SystemExit(main())
