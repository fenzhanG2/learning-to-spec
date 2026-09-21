import json
from pathlib import Path

from .reduction import apply_review, digest, load_review, recommended_decisions, reduced_export, suggested_action
from .privacy_presentation import present_review
from .storage import write_json
from .story_pipeline import run_story
from .delivery import deliver, preferences
from .privacy import content_redaction


COMMANDS = {"privacy-scan", "privacy-choose", "private-story", "studio", "share-package", "share-approve", "artifact-plan", "artifact-publish", "artifact-verify"}


def configure(subcommands):
    scan = subcommands.add_parser("privacy-scan", help="Abstract the session privately, then review the selected draft documents for disclosure")
    scan.add_argument("session")
    scan.add_argument("--out", type=Path, required=True)
    scan.add_argument("--audience", default="local", help="local, root (owner-only), or team:SLUG")
    scan.add_argument("--readers", choices=("human", "agent", "both"), required=True)
    scan.add_argument("--delivery", choices=("local", "artifactstore"), required=True)
    scan.add_argument("--purpose", default="Technical story and actionable Agent handoff")
    scan.add_argument("--redact", action="append", default=[], help="Additional exact phrase to flag everywhere")
    scan.add_argument("--allow-copilot-review", action="store_true", help="Consent to send the complete observable session to Copilot for private abstraction, then review draft disclosures with rules plus Copilot")
    scan.add_argument("--max-calls", type=int, default=20)
    scan.add_argument("--model")
    scan.add_argument("--gh-host")
    choose = subcommands.add_parser("privacy-choose", help="Choose how to handle each finding; unresolved choices block generation")
    choose.add_argument("directory", type=Path)
    choose.add_argument("--recommended", action="store_true", help="Adopt available suggestions; uncertain semantic findings remain unresolved and block generation")
    choose.add_argument("--out", type=Path, required=True)
    generate = subcommands.add_parser("private-story", help="Apply approved disclosure choices to the private abstraction and export HTML/Markdown")
    generate.add_argument("directory", type=Path)
    generate.add_argument("--decisions", type=Path, required=True)
    generate.add_argument("--out", type=Path, required=True)
    generate.add_argument("--resume", action="store_true", help="Resume a failed generation only when the source, audience and choices are unchanged")
    generate.add_argument("--confirm-choices", action="store_true", help="Explicitly confirm the reviewed keep/remove/pseudonymize/generalize choices")
    generate.add_argument("--max-calls", type=int, default=24)
    generate.add_argument("--model")
    generate.add_argument("--gh-host")
    studio = subcommands.add_parser("studio", help="Run the local, private review studio for Copilot App and CLI")
    studio.add_argument("--session", help="Preselect one session path/ID instead of listing local sessions")
    studio.add_argument("--out", type=Path, default=Path.home() / ".learning-to-spec/studio")
    studio.add_argument("--port", type=int, default=0)
    studio.add_argument("--open-generation", type=Path, help="Reopen validated private-story output without new model calls")
    studio.add_argument("--review", type=Path, help="Matching private review directory for --open-generation")
    studio.add_argument("--model")
    studio.add_argument("--gh-host")
    studio.add_argument("--max-calls", type=int, default=24)
    package = subcommands.add_parser("share-package", help="Build an allowlisted sharing package; never includes private review/source/cache files")
    package.add_argument("directory", type=Path)
    package.add_argument("--out", type=Path, required=True)
    package.add_argument("--audience", required=True)
    package.add_argument("--readers", choices=("human", "agent", "both"), required=True)
    package.add_argument("--without-evidence", action="store_true")
    approve = subcommands.add_parser("share-approve", help="Record explicit review of exact final package bytes")
    approve.add_argument("directory", type=Path)
    approve.add_argument("--package-id", required=True)
    approve.add_argument("--acknowledge", nargs="*", default=[])
    approve.add_argument("--reviewed-all-files", action="store_true")
    plan = subcommands.add_parser("artifact-plan", help="Read the current ArtifactStore destination policy; never uploads")
    plan.add_argument("directory", type=Path)
    plan.add_argument("--site", required=True)
    plan.add_argument("--out", type=Path, required=True)
    publish = subcommands.add_parser("artifact-publish", help="Upload a privacy-approved package after explicit destination confirmation")
    publish.add_argument("directory", type=Path)
    publish.add_argument("--plan", type=Path, required=True)
    publish.add_argument("--confirm", required=True)
    verify = subcommands.add_parser("artifact-verify", help="Read back a recorded publication without uploading or changing remote access")
    verify.add_argument("directory", type=Path)
    verify.add_argument("--plan", type=Path, required=True)


def generate_private(directory, decisions, output, home, resume=False, confirm_choices=False, **settings):
    if confirm_choices is not True:
        raise ValueError("Explicit confirmation of the privacy choices is required before generation")
    review, baseline = load_review(directory)
    if review.get("pipeline") not in {None, "abstract-then-redact/v1"}:
        raise ValueError("Unknown privacy pipeline; old generation cannot be used as a fallback")
    if review.get("pipeline") == "abstract-then-redact/v1":
        from .abstract_privacy import generate_abstract_private

        return generate_abstract_private(directory, decisions, output, home, resume=resume, confirm_choices=confirm_choices, **settings)
    selection = review.get("preferences", {})
    preferences(selection.get("readers"), selection.get("destination"), review["audience"])
    output = Path(output).resolve()
    if output.exists() and any(output.iterdir()):
        if not resume:
            raise ValueError("Use a new private generation directory or explicitly resume unchanged choices")
        review, baseline = load_review(directory)
        receipt = json.loads((output / "reduced/reduction.json").read_bytes())
        if receipt.get("review_id") != review["review_id"] or receipt.get("decisions_sha256") != digest(decisions) or receipt.get("audience") != review["audience"]:
            raise ValueError("Source, audience or choices changed; use a new generation directory")
    else:
        apply_review(directory, decisions, output / "reduced")
    canonical = reduced_export(output / "reduced", home)
    full_content = review.get("privacy_mode") == "full"
    with content_redaction(not full_content):
        result = run_story(None, home, output / "story", from_export=canonical,
                           resume=resume and (output / "story/_support/story-source.json").is_file(), **settings)
    result["privacy"] = ("No privacy scan or sensitive-content redaction requested. Generated files may contain private information or credentials."
                         if full_content else "Generated only from the reduced session; source unchanged. Inspect final output before sharing.")
    result.update(deliver(output, selection))
    for key, filename in (("human_spec", "human-spec.html"), ("agent_spec", "agent-spec.md"), ("evidence", "evidence.md")):
        result.pop(key, None)
        if filename in result["files"]:
            result[key] = str(output / "deliverables" / filename)
    return result


def run(arguments, defaults):
    command = arguments.command
    settings = {"model": getattr(arguments, "model", None) or defaults.get("model"), "gh_host": getattr(arguments, "gh_host", None) or defaults.get("gh_host")}
    if command == "privacy-scan":
        from .abstract_privacy import prepare_abstract_review

        if not arguments.allow_copilot_review:
            raise ValueError("Abstract-first preparation requires explicit Copilot consent; use --allow-copilot-review after reviewing its full-context disclosure")
        selection = preferences(arguments.readers, arguments.delivery, arguments.audience)
        review = prepare_abstract_review(arguments.session, arguments.home, arguments.out, arguments.audience, selection, "llm",
                                         custom=arguments.redact, purpose=arguments.purpose, max_calls=arguments.max_calls, **settings)
        return {"review_id": review["review_id"], "review": str(arguments.out / "review.json"), "findings": len(review["findings"]),
                "hard_removals": review["hard_removals"], "semantic": present_review(review)["semantic"], "pipeline": review["pipeline"],
                "next": "Approve disclosures in the already abstracted documents. Private-story applies those choices and renders without another model generation."}
    if command == "privacy-choose":
        review, baseline = load_review(arguments.directory)
        decisions = recommended_decisions(review)
        if not arguments.recommended:
            for finding, visible in zip(review["findings"], present_review(review, baseline)["findings"]):
                print(f'\n{visible["label"]} · {len(visible["occurrences"])} occurrences\n{visible["text"]}\n{visible["reason"]}')
                if visible.get("assessment_summary"):
                    print(visible["assessment_summary"]["notice"])
                for occurrence in visible["occurrences"]:
                    print(f'Selected span · Event {occurrence["path"][0] + 1} · {json.dumps(occurrence["path"][1:])} · characters {occurrence["start"]}:{occurrence["end"]}')
                for context in visible.get("contexts", []):
                    print(f'Source context:\n{context}')
                for context in visible.get("related_contexts", []):
                    print(f'Related clue (context only) · Event {context["event"]} · {json.dumps(context["field"])}\n{context["text"]}')
                action = input(f'Choose keep / remove / pseudonymize / generalize (suggested: {suggested_action(finding)}): ').strip()
                if action not in {"keep", "remove", "pseudonymize", "generalize"}:
                    raise ValueError("No valid explicit choice; no decisions were saved")
                decisions["choices"][finding["id"]] = {"action": action}
                if action == "generalize":
                    decisions["choices"][finding["id"]]["replacement"] = input("Replacement that preserves the technical meaning: ")
        write_json(arguments.out, decisions)
        return {"decisions": str(arguments.out), "findings": len(review["findings"]), "unresolved": len(review["findings"]) - len(decisions["choices"]), "note": "Only the reviewed source/audience can use these decisions. Unresolved items require individual choices."}
    if command == "private-story":
        return generate_private(arguments.directory, json.loads(arguments.decisions.read_bytes()), arguments.out, arguments.home, resume=arguments.resume, confirm_choices=arguments.confirm_choices, max_calls=arguments.max_calls, **settings)
    if command == "studio":
        from .studio import serve
        serve(arguments.home, arguments.out, arguments.session, arguments.port, arguments.open_generation, arguments.review, max_calls=arguments.max_calls, **settings)
        return {"status": "studio_stopped"}
    if command == "share-package":
        from .share_package import prepare_package
        manifest = prepare_package(arguments.directory, arguments.out, arguments.audience, not arguments.without_evidence, arguments.readers)
        return {"package": str(arguments.out), "package_id": manifest["package_id"], "findings": len(manifest["findings"]), "manifest": str(arguments.out / "manifest.json")}
    if command == "share-approve":
        from .share_package import approve_package
        return approve_package(arguments.directory, arguments.package_id, arguments.acknowledge, arguments.reviewed_all_files)
    from .artifacts import ArtifactClient, destination_plan, publish_package, verify_publication
    client = ArtifactClient()
    if command == "artifact-plan":
        plan = destination_plan(arguments.directory, arguments.site, client)
        write_json(arguments.out, plan)
        return plan
    if command == "artifact-verify":
        return verify_publication(arguments.directory, json.loads(arguments.plan.read_bytes()), client)
    return publish_package(arguments.directory, json.loads(arguments.plan.read_bytes()), arguments.confirm, client)
