import re
from collections import Counter


def prose(text):
    text = re.sub(r"```[\s\S]*?```|`[^`\n]*`|https?://\S+", " ", text)
    return re.sub(r"(?m)^\s*(?:[+\->]|\w+[\\/]).*$", " ", text)


def resolve_language(events, requested="auto"):
    if requested != "auto":
        if not re.fullmatch(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*", requested):
            raise ValueError("Language must be auto or a BCP-47 language tag")
        return {"language": requested, "requested": requested, "method": "explicit", "votes": {}}
    votes = Counter()
    source = [event.get("human_input", "") for event in events if event.get("human_input")]
    if not source:
        source = [event.get("text", "") for event in events if event.get("type") == "assistant.message"]
    for content in source:
        text = prose(content)
        counts = {"ja": len(re.findall(r"[\u3040-\u30ff]", text)), "ko": len(re.findall(r"[\uac00-\ud7af]", text)),
                  "zh-CN": len(re.findall(r"[\u3400-\u9fff]", text)), "ru": len(re.findall(r"[\u0400-\u04ff]", text)),
                  "ar": len(re.findall(r"[\u0600-\u06ff]", text)), "en": len(re.findall(r"\b[A-Za-z]{2,}\b", text))}
        if counts["ja"] >= 3:
            counts["ja"] += counts.pop("zh-CN")
        if max(counts.values()) > 0:
            votes[max(counts, key=counts.get)] += 1
    language = votes.most_common(1)[0][0] if votes else "en"
    return {"language": language, "requested": "auto", "method": "source-prose-majority/v1", "votes": dict(votes),
            "limit": "Heuristic for presentation metadata and fixed UI labels only, not a generation-language instruction. Mixed and unsupported Latin-script languages may be misidentified."}


def language_contract(language="auto"):
    if language == "auto":
        return ("\n\nSOURCE_LANGUAGE_POLICY\nNaturally follow the language of the source session's human conversation in both reader editions, "
                "including short route labels, route descriptions, titles, diagram captions and conclusions. No target language is prescribed. Preserve meaningful language switching and technical terminology. "
                "Do not take the language of these instructions, tool logs, or auxiliary drafts as the language to write in. "
                "Keep source quotations, code, commands, identifiers and schema enums unchanged. Do not translate historical command arguments.\n")
    return ("\n\nOUTPUT_LANGUAGE_CONTRACT\nThe output language is " + language + ". Write BOTH human prose and Agent handoff in this language, "
            "including titles, diagrams and conclusions. The language of these instructions is NOT the output language. "
            "Keep source quotations, code, commands, identifiers and schema enums unchanged. Do not translate historical command arguments. "
            "Use natural sentences; English is measured in words, not Chinese characters.\n")


def source_script_issues(edition, events):
    source = prose("\n".join(event.get("human_input", "") for event in events if event.get("human_input")))
    source_latin = len(re.findall(r"[A-Za-z]", source))
    source_cjk = len(re.findall(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]", source))
    if source_latin < 200 or source_cjk:
        return []
    problems = []
    source_literals = "\n".join(str(event.get(key, "")) for event in events for key in ("human_input", "text"))

    def inspect(value, path):
        if isinstance(value, dict):
            for key, nested in value.items():
                if key not in {"refs", "tool_refs", "human_refs", "human_input_coverage", "period"}:
                    inspect(nested, path + "/" + key)
        elif isinstance(value, list):
            for index, nested in enumerate(value):
                inspect(nested, path + "/" + str(index))
        elif isinstance(value, str):
            narrative = prose(value)
            cjk = len(re.findall(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]", narrative))
            latin = len(re.findall(r"[A-Za-z]", narrative))
            short_route = re.fullmatch(r"/article/route/\d+/(?:title|detail)", path)
            source_literal = narrative.strip() and narrative.strip() in source_literals
            if not source_literal and ((cjk >= 40 and cjk > latin * 0.7)
                                       or (short_route and cjk >= 3 and cjk > latin * 0.3)):
                problems.append(path)

    inspect(edition, "")
    if problems:
        return ["Authored prose does not follow the source human conversation's writing system at " + ", ".join(problems[:12])
                + ". Read the original human messages to choose the natural narrative language; do not follow exporter instructions or auxiliary drafts. Repair all affected Human/Agent narrative fields, preserving literal source quotations, code and technical identifiers. No target language tag is prescribed."]
    return []


def validate_language(edition, language, events=None):
    if language == "auto" and events is not None:
        return source_script_issues(edition, events)
    if language != "en":
        return []
    article = edition.get("article", {})
    values = [article.get("title", ""), article.get("subtitle", ""), article.get("agent_markdown", "")]
    values += [chapter.get("markdown", "") for chapter in article.get("chapters", [])]
    values += [item.get("text", "") for item in edition.get("brief", {}).values() if isinstance(item, dict)]
    values += edition.get("insights", {}).get("closing", {}).get("paragraphs", [])
    for value in values:
        text = prose(value)
        chinese = len(re.findall(r"[\u3400-\u9fff]", text))
        latin = len(re.findall(r"[A-Za-z]", text))
        if chinese > 30 and chinese > latin * 0.2:
            return ["English output requested, but substantial authored prose is Chinese; translate prose, not source quotes or code"]
    return []
