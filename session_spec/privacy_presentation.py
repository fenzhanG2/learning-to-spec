import copy

from .reduction_rules import CATEGORIES
from .reduction import at_path, strings, suggested_action


QUESTIONS = {
    "identifier": "Could the highlighted text identify or contact someone?",
    "environment": "Could the highlighted workspace details link this work to a person or organization?",
    "health": "Does the highlighted text contain personal medical information rather than technical or fictional data?",
    "financial": "Does the highlighted text contain personal financial information rather than product or test data?",
    "personal_life": "Does this include private personal context that this audience does not need?",
    "reputation": "Would you prefer to omit this aside while retaining the technical failure, correction or lesson?",
    "confidential": "Is this third-party or business detail appropriate for the selected audience?",
    "inference": "Could these selected and related details become linkable when shared together?",
}


def assessment_summary(finding):
    assessments = finding.get("assessments")
    if assessments is None or assessments == []:
        return {"status": "unavailable", "count": 0, "notice": "Separate model assessments are unavailable; aggregate uncertainty does not establish disagreement."}
    if not isinstance(assessments, list) or any(not isinstance(item, dict) or not isinstance(item.get("necessity"), str) or item["necessity"] not in {"necessary", "unnecessary", "uncertain"} for item in assessments):
        return {"status": "invalid", "count": None, "notice": "Assessment details cannot be interpreted; do not infer agreement or disagreement."}
    necessities = sorted({item["necessity"] for item in assessments})
    count = len(assessments)
    if len(necessities) > 1:
        status, notice = "disagreement", "Model assessments disagree on task relevance; decide for this exact context."
    elif necessities == ["uncertain"]:
        status, notice = "uncertain", "All recorded model assessments are uncertain about task relevance."
    else:
        status, notice = "agreement", "Recorded model assessments agree on task relevance, but this is not a source fact or a privacy guarantee."
    return {"status": status, "count": count, "necessities": necessities, "notice": f"{count} recorded assessment(s). {notice}"}


def present_finding(finding):
    if "copilot" not in finding.get("detectors", []) and "scope" not in finding:
        return copy.deepcopy(finding)
    visible = {key: copy.deepcopy(finding[key]) for key in (
        "id", "category", "text", "necessity", "recommended", "detectors", "occurrences", "related", "scope",
        "contexts", "related_contexts") if key in finding}
    categories = sorted({category for category in finding.get("categories", [finding.get("category")]) if category in QUESTIONS})
    visible["categories"] = categories
    visible["label"] = "Check possible " + (" / ".join(CATEGORIES[category]["label"].lower() for category in categories) or "disclosure")
    origin = "Model suggestion" if "copilot" in finding.get("detectors", []) else "Detection suggestion"
    visible["reason"] = (origin + ", not a fact about anyone. " + (" ".join(QUESTIONS[category] for category in categories) or "Is this detail appropriate for the selected audience?")
                         + " Decide from the exact text and your purpose. Related clues are context, not automatic removal targets.")
    visible["assessment_summary"] = assessment_summary(finding)
    visible["alternative"] = ""
    visible["presentation"] = "bounded-disclosure-questions/v1"
    return visible


def present_review(review, baseline=None):
    visible = copy.deepcopy(review)
    visible["findings"] = [present_finding(finding) for finding in review["findings"]]
    if baseline is not None:
        slots = {f"S{number}": (path, text) for number, (path, text) in enumerate(strings(baseline), 1)}
        for original, finding in zip(review["findings"], visible["findings"]):
            finding["recommended"] = suggested_action(original)
            summary = assessment_summary(original)
            if "scope" in original and summary.get("status") == "agreement":
                necessity = original.get("necessity")
                if summary.get("necessities") == [necessity]:
                    if necessity == "necessary":
                        finding["recommended"] = "keep"
                    elif necessity == "unnecessary":
                        categories = set(original.get("categories", [original.get("category")]))
                        if categories and categories <= {"identifier", "environment"}:
                            finding["recommended"] = "pseudonymize"
                        elif categories and categories <= {"personal_life", "reputation", "health", "financial"}:
                            finding["recommended"] = "remove"
            finding["contexts"] = []
            for occurrence in finding["occurrences"][:3]:
                text = at_path(baseline, occurrence["path"])
                finding["contexts"].append(text[max(0, occurrence["start"] - 160):occurrence["end"] + 160])
            finding["related_contexts"] = []
            for identifier in finding.get("related", []):
                if identifier in slots:
                    path, text = slots[identifier]
                    finding["related_contexts"].append({"event": path[0] + 1, "field": list(path[1:]), "text": text})
    semantic = visible.get("semantic", {})
    if semantic.get("status") == "reviewed":
        semantic["limitations"] = ["Model suggestions can miss disclosures or misclassify technical details. Review exact spans; no anonymity or completeness guarantee."]
    return visible
