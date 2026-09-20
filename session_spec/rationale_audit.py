from .source_excerpt import excerpt_segments, payload_text, source_payload
from .story_grounding import quote_basis, source_quote_diagnostic, source_quote_origins


SCHEMA = "rationale-audit/v2"
ASSESSMENTS = {"expressed_reason", "inference", "action_or_result_only", "retrospective", "unsupported"}
MAX_SOURCE_CHARS = 12000


def rationale_focus(edition, events):
    evidence = {event["ref"]: event for event in events}
    positions = {event["ref"]: index for index, event in enumerate(events)}
    phases = edition.get("article", {}).get("agent_detail", {}).get("trajectory", [])
    rows = []
    for index, phase in enumerate(phases):
        rationale = phase.get("rationale", {})
        if rationale.get("basis") not in {"recorded", "inferred"}:
            continue
        later = phases[index + 1:]
        boundaries = [ref for following in later for ref in following.get("refs", []) if ref in positions]
        boundary = min(boundaries, key=positions.get) if boundaries else None
        current_anchors = [ref for ref in phase.get("refs", []) if ref in positions]
        overlaps = boundary is not None and any(positions[ref] >= positions[boundary] for ref in current_anchors)
        if overlaps:
            boundary = None
        references = list(dict.fromkeys(rationale.get("refs", [])))
        rows.append({"path": f"/article/agent_detail/trajectory/{index}/rationale", "phase": phase.get("id"),
                     "basis": rationale["basis"], "claim": rationale.get("text"), "refs": references,
                     "next_phase_start": boundary, "overlapping_phase_anchors": overlaps,
                     "later_refs": [ref for ref in references if ref in positions and boundary is not None and positions[ref] >= positions[boundary]]})
    sources, omitted, remaining = [], [], MAX_SOURCE_CHARS
    for reference in dict.fromkeys(ref for row in rows for ref in row["refs"] if ref in evidence):
        if remaining <= 0:
            omitted.append(reference)
            continue
        event = evidence[reference]
        text = payload_text(source_payload(event))
        limit = min(1200, remaining)
        segments = excerpt_segments(text, limit)
        remaining -= sum(len(segment["text"]) for segment in segments)
        sources.append({"ref": reference, "type": event.get("type"), "human_authority": bool(event.get("human_input")),
                        "quote_origins": source_quote_origins(event),
                        "tool": event.get("tool"), "characters": len(text), "truncated": len(text) > limit, "segments": segments})
    return {"schema": SCHEMA, "rationales": rows, "sources": sources, "omitted_source_refs": omitted,
            "limit": "Every retained phase rationale is audited; not_recorded has no claimed rationale. Quotes are from reduced observable payloads only. "
                     "The next phase boundary uses incomplete authored source anchors, not a hard phase extent or proof of backdating. "
                     "A cited transition can close one decision and open the next; potential later refs require explicit temporal adjudication. "
                     "Overlapping anchors have no automatic cutoff; review their real chronology rather than infer one. "
                     "Excerpt limits and omitted source refs are explicit; consult the full historical events before deciding. "
                     "Literal support and a model assessment are not an entailment proof."}


def validate_rationale_audit(review, focus, events):
    if not isinstance(review, dict):
        return ["Rationale audit review must be an object"]
    rows = {row["path"]: row for row in focus["rationales"]}
    decisions = review.get("rationale_audit", [])
    if (not isinstance(decisions, list) or len(decisions) != len(rows)
            or any(not isinstance(item, dict) or not isinstance(item.get("path"), str) or item["path"] not in rows for item in decisions)
            or len({item["path"] for item in decisions}) != len(rows)):
        return ["Rationale audit must cover every indexed rationale path exactly once"]
    evidence = {event["ref"]: event for event in events}
    errors = []
    for item in decisions:
        row = rows[item["path"]]
        prefix = "Rationale audit " + item["path"] + ": "
        assessment = item.get("assessment")
        status = item.get("status")
        if not isinstance(assessment, str) or assessment not in ASSESSMENTS or status not in ("supported", "needs_fix"):
            errors.append(prefix + "invalid assessment or status")
        if not isinstance(item.get("note"), str) or not item["note"].strip():
            errors.append(prefix + "explain what the cited source actually expresses, not just whether an action occurred")
        support = item.get("evidence")
        if not isinstance(support, list) or len(support) > 3 or (status == "supported" and not support):
            errors.append(prefix + "provide 1-3 literal source spans for supported; up to 3 for needs_fix")
            continue
        for span in support:
            if not isinstance(span, dict):
                errors.append(prefix + "invalid source span")
                continue
            reference, origin, quote = span.get("ref"), span.get("origin"), span.get("quote")
            source = evidence.get(reference) if isinstance(reference, str) else None
            if (source is None or origin not in ("human", "assistant", "tool", "context")
                    or not isinstance(quote, str) or len(quote) > 500 or not quote_basis(source, origin, quote)):
                if source is None or not isinstance(quote, str) or len(quote) > 500:
                    errors.append(prefix + "source span needs a known reference and a nonempty literal string of at most 500 characters")
                else:
                    errors.append(prefix + source_quote_diagnostic(source, origin, quote))
            if status == "supported" and reference not in row["refs"]:
                errors.append(prefix + "support is absent from the rationale's declared refs; repair the authored record first")
        if status == "supported" and row["basis"] == "recorded" and any(isinstance(span, dict) and span.get("ref") in row["later_refs"] for span in support):
            timing = item.get("timing")
            if (not isinstance(timing, dict) or timing.get("assessment") not in ("shared_transition", "overlapping_phase")
                    or not isinstance(timing.get("note"), str) or not timing["note"].strip()):
                errors.append(prefix + "potential later-phase source requires an explicit timing assessment and note: shared_transition or overlapping_phase "
                              "can support a correctly attributed reason; retrospective/uncertain cannot establish an earlier recorded motive. "
                              "Aggregate citations are not exclusive phase boundaries; do not invent a defect merely to satisfy this check")
        if status == "supported":
            required = "expressed_reason" if row["basis"] == "recorded" else "inference"
            if assessment != required:
                errors.append(prefix + row["basis"] + " requires " + required + "; actions/results alone do not record a motive")
        if status == "needs_fix":
            findings = review.get("issues", [])
            if not any(isinstance(issue, dict) and isinstance(issue.get("path"), str)
                       and (issue["path"] == row["path"] or issue["path"].startswith(row["path"] + "/")) for issue in findings):
                errors.append(prefix + "needs_fix requires a grounded issue at this rationale or one of its fields")
    return errors
