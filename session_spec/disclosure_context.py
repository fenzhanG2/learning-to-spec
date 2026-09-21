import re

from .reduction_rules import SENTENCES, private_clause_end


FACETS = {
    "workplace": re.compile(r"\b(?:I (?:work (?:as|at|for)|am the (?:only|sole))|I'm the (?:only|sole)|my (?:employer|workplace|job title|office))\b|我(?:是唯一|在.{1,24}(?:工作|任职))|我的(?:雇主|工作单位|职位)", re.I),
    "location": re.compile(r"\b(?:I (?:live|reside|grew up|commute)|my (?:home town|hometown|neighbou?rhood|home address|village|apartment))\b|我(?:住在|居住|家在|每天通勤)|我的(?:住址|家乡|小区)", re.I),
    "schedule": re.compile(r"\b(?:I (?:attend|visit|leave|arrive|volunteer|take the train|catch the bus).{0,65}\b(?:every|each|on|at)|my (?:shift|appointment|commute|weekly routine))\b|我(?:每周|每天|每月).{1,35}(?:去|到|参加|值班)|我的(?:值班|日程|预约)", re.I),
    "affiliation": re.compile(r"\b(?:I (?:am|was) (?:a |the )?(?:member|graduate|winner|finalist)|my (?:alma mater|school|university|club|congregation))\b|我是.{1,24}(?:校友|会员|获奖者)|我的(?:母校|学校|社团)", re.I),
    "household": re.compile(r"\bmy (?:spouse|partner|wife|husband|daughter|son|child|children|parent|mother|father)\b|我的(?:配偶|伴侣|妻子|丈夫|女儿|儿子|孩子|父母)", re.I),
}


def local_combinations(fields):
    clues = []
    for identifier, path, text in fields:
        fenced = False
        offset = 0
        for line in text.splitlines(keepends=True):
            if line.lstrip().startswith(("```", "~~~")):
                fenced = not fenced
            elif not fenced and not line.lstrip().startswith((">", '"', "'", "`")):
                for sentence in SENTENCES.finditer(line):
                    start = offset + sentence.start()
                    end = private_clause_end(text, start, offset + sentence.end())
                    start += len(text[start:end]) - len(text[start:end].lstrip())
                    value = text[start:end]
                    facets = {name for name, pattern in FACETS.items() if pattern.search(value)}
                    if facets:
                        clues.append({"slot": identifier, "path": path, "start": start, "end": end, "facets": facets})
            offset += len(line)
    if len({clue["path"][0] for clue in clues}) < 2 or len(set().union(*(clue["facets"] for clue in clues))) < 2:
        return []
    related = list(dict.fromkeys(clue["slot"] for clue in clues))[:13]
    return [{**clue, "related": [slot for slot in related if slot != clue["slot"]][:12]} for clue in clues]
