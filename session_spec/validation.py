from .ingest import human_input


def references(value):
    result = set()
    if isinstance(value, dict):
        for key, content in value.items():
            if key == "refs" and isinstance(content, list):
                result.update(entry for entry in content if isinstance(entry, str))
            else:
                result.update(references(content))
    elif isinstance(value, list):
        for content in value:
            result.update(references(content))
    return result


def calibrate_next_basis(spec, records):
    human_refs = {record["ref"] for record in records if human_input(record)}
    warnings = []
    if not isinstance(spec, dict) or not isinstance(spec.get("next_run"), dict):
        return warnings
    for section in ("steps", "acceptance"):
        entries = spec["next_run"].get(section)
        if not isinstance(entries, list):
            continue
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict) or not isinstance(entry.get("refs"), list):
                continue
            if entry.get("basis") in {"explicit", "user_requirement"} and not any(ref in human_refs for ref in entry["refs"] if isinstance(ref, str)):
                previous = entry["basis"]
                entry["basis"] = "proposed"
                warnings.append(f"next_run.{section}[{index}] 未引用真人输入，依据从 {previous} 降为 proposed；未添加用户授权或修改行动内容。")
    return warnings


def validate_spec(spec, records, require_coverage=True):
    issues = []
    lookup = {record["ref"]: record for record in records}
    requests = {record["ref"] for record in records if human_input(record)}

    def issue(section, message, refs=None):
        issues.append({"severity": "error", "section": section, "message": message, "refs": refs or []})

    def fields(value, names, section):
        if not isinstance(value, dict):
            issue(section, "Expected an object.")
            return False
        valid = True
        text_fields = {"title", "text", "goal", "action", "observation", "decision", "why", "status", "attribution", "quote", "check", "result", "scope", "reason", "verification", "basis", "when", "validation", "criterion", "method", "question", "ref", "disposition", "summary"}
        for name in names:
            if name not in value:
                issue(section, "Missing field: " + name)
                valid = False
            elif name in text_fields and not (name == "verification" and section == "this_run") and not isinstance(value[name], str):
                issue(section, name + " must be a string.")
                valid = False
        return valid

    def items(parent, name, required, section):
        value = parent.get(name) if isinstance(parent, dict) else None
        if not isinstance(value, list):
            issue(section, "Expected an array.")
            return []
        valid = []
        for index, entry in enumerate(value):
            if fields(entry, required + ["refs"], f"{section}[{index}]"):
                if not isinstance(entry["refs"], list) or not all(isinstance(ref, str) for ref in entry["refs"]):
                    issue(section, "refs must be a string array.")
                    continue
                valid.append(entry)
        return valid

    def grounded(value, section, allow_empty=False):
        if not isinstance(value, dict):
            return
        refs = value.get("refs")
        if not isinstance(refs, list) or not all(isinstance(ref, str) for ref in refs):
            issue(section, "refs must be a string array.")
        elif not refs and not allow_empty:
            issue(section, "Historical claims need source references.")
        elif any(ref not in lookup for ref in refs):
            issue(section, "Unknown source reference.", [ref for ref in refs if ref not in lookup])

    if not fields(spec, ["title", "objective", "trajectory", "requirements", "this_run", "next_run", "open_questions", "request_coverage"], "root"):
        return issues
    if not isinstance(spec["title"], str) or not spec["title"].strip():
        issue("title", "Title must be a nonempty string.")
    if fields(spec["objective"], ["text", "refs"], "objective"):
        grounded(spec["objective"], "objective")
    trajectory = items(spec, "trajectory", ["title", "goal", "action", "observation", "decision", "why"], "trajectory")
    if not trajectory:
        issue("trajectory", "A work spec must retain the target session trajectory.")
    for episode in trajectory:
        grounded(episode, "trajectory")
    requirements = items(spec, "requirements", ["text", "status", "attribution"], "requirements")
    for requirement in requirements:
        grounded(requirement, "requirements")
        if requirement["status"] not in {"active", "superseded", "deferred", "rejected", "unclear"}:
            issue("requirements", f"Invalid requirement status {requirement['status']!r}; use active, superseded, deferred, rejected or unclear. Completion is not a requirement lifecycle status.", requirement["refs"])
        if requirement["attribution"] not in {"user", "agent", "inferred"}:
            issue("requirements", f"Invalid attribution {requirement['attribution']!r}; use user, agent or inferred.", requirement["refs"])
        if requirement["attribution"] == "user":
            quote = requirement.get("quote", "")
            sources = [human_input(lookup[ref]) for ref in requirement["refs"] if ref in requests]
            if not isinstance(quote, str) or not quote.strip() or not any(quote in source for source in sources):
                issue("requirements", "User attribution needs an exact quote in a cited human message or explicit ask_user response, not the agent's proposed choices.", requirement["refs"])
            elif requirement["status"] == "superseded":
                original_lines = [lookup[ref]["line"] for ref in requirement["refs"] if ref in requests and quote in human_input(lookup[ref])]
                later_human = any(ref in requests and lookup[ref]["line"] > min(original_lines) and quote not in human_input(lookup[ref]) for ref in requirement["refs"])
                if not later_human:
                    issue("requirements", "Superseded user requirements must also cite a later human instruction that replaces them. Completion, additive requirements and assistant-only changes do not supersede user intent; use active or unclear if no replacement is evidenced.", requirement["refs"])
    if fields(spec["this_run"], ["work", "verification", "boundaries"], "this_run"):
        work = items(spec["this_run"], "work", ["text", "status"], "this_run.work")
        checks = items(spec["this_run"], "verification", ["check", "result", "scope"], "this_run.verification")
        for entry in work:
            grounded(entry, "this_run.work")
            if entry["status"] not in {"verified", "reported", "partial", "failed", "unknown"}:
                issue("this_run.work", f"Invalid work status {entry['status']!r}; use verified, reported, partial, failed or unknown.", entry["refs"])
            if entry["status"] == "verified" and not any(lookup.get(ref, {}).get("type") == "tool.execution_complete" and lookup[ref].get("success") is True for ref in entry["refs"]):
                issue("this_run.work", "Verified work requires observed tool-result evidence; assistant claims are only reported.", entry["refs"])
        for entry in checks:
            grounded(entry, "this_run.verification")
            if entry["result"] not in {"passed", "failed", "not_run", "inconclusive", "reported"}:
                issue("this_run.verification", f"Invalid check result {entry['result']!r}; use passed, failed, not_run, inconclusive or reported.", entry["refs"])
            if entry["result"] in {"passed", "failed"} and not any(lookup.get(ref, {}).get("type") == "tool.execution_complete" for ref in entry["refs"]):
                issue("this_run.verification", "Observed pass/fail requires tool-result evidence; otherwise use reported/inconclusive.", entry["refs"])
            if entry["result"] == "passed" and not any(lookup.get(ref, {}).get("type") == "tool.execution_complete" and lookup[ref].get("success") is True for ref in entry["refs"]):
                issue("this_run.verification", "A failed tool call cannot be the only evidence for a passed check.", entry["refs"])
        for entry in items(spec["this_run"], "boundaries", ["text"], "this_run.boundaries"):
            grounded(entry, "this_run.boundaries")
    next_run = spec["next_run"]
    if fields(next_run, ["resume_from", "steps", "reuse", "acceptance", "boundaries"], "next_run"):
        if fields(next_run["resume_from"], ["text", "refs"], "next_run.resume_from"):
            grounded(next_run["resume_from"], "next_run.resume_from")
        for name, required in [
            ("steps", ["action", "reason", "verification", "basis"]),
            ("reuse", ["when", "procedure", "avoid", "validation", "basis"]),
            ("acceptance", ["criterion", "method", "basis"]),
            ("boundaries", ["text"]),
        ]:
            for entry in items(next_run, name, required, "next_run." + name):
                grounded(entry, "next_run." + name, allow_empty=entry.get("basis") == "proposed")
                allowed = {"steps": {"explicit", "proposed"}, "reuse": {"observed_success", "proposed"}, "acceptance": {"user_requirement", "proposed"}}
                if name in allowed and entry["basis"] not in allowed[name]:
                    issue("next_run." + name, f"Invalid future basis {entry['basis']!r}; use {', '.join(sorted(allowed[name]))}.", entry["refs"])
                if name == "reuse" and (not isinstance(entry["procedure"], list) or not isinstance(entry["avoid"], list)):
                    issue("next_run.reuse", "procedure and avoid must be arrays.")
                if entry.get("basis") in {"explicit", "user_requirement"} and not any(ref in requests for ref in entry["refs"]):
                    issue("next_run." + name, "An explicit user requirement must cite a root user message.", entry["refs"])
                if entry.get("basis") == "observed_success" and not any(lookup.get(ref, {}).get("type") == "tool.execution_complete" and lookup[ref].get("success") is True for ref in entry["refs"]):
                    issue("next_run.reuse", "Observed-success methods require a successful tool result; otherwise mark proposed.", entry["refs"])
    for entry in items(spec, "open_questions", ["question", "why"], "open_questions"):
        grounded(entry, "open_questions")
    coverage = spec.get("request_coverage")
    if not isinstance(coverage, list):
        issue("request_coverage", "Expected an array.")
    else:
        refs = []
        for entry in coverage:
            if fields(entry, ["ref", "disposition", "summary"], "request_coverage"):
                refs.append(entry["ref"])
                if entry["disposition"] not in {"requirement", "correction", "question", "control", "context"}:
                    issue("request_coverage", "Invalid request disposition.")
        if len(refs) != len(set(refs)):
            issue("request_coverage", "Duplicate root request references.")
        if require_coverage and set(refs) != requests:
            issue("request_coverage", "Missing or invented root requests.", sorted(requests.symmetric_difference(refs)))
    return issues


def order_spec(spec):
    spec["request_coverage"].sort(key=lambda item: item["ref"])
    return spec
