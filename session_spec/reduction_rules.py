import re


CATEGORIES = {
    "identifier": {"label": "Personal identifiers", "recommend": "pseudonymize", "why": "Contact details and identity can link this story to a person."},
    "environment": {"label": "Linkable workspace details", "recommend": "pseudonymize", "why": "Usernames, internal hosts and network addresses may identify a person or organization."},
    "health": {"label": "Personal health disclosure", "recommend": "remove", "why": "A personal medical detail is not usually necessary for a technical handoff."},
    "financial": {"label": "Personal financial disclosure", "recommend": "remove", "why": "Personal income, debt or account details may be unnecessary to the task."},
    "personal_life": {"label": "Private life / protected attributes", "recommend": "remove", "why": "This may reveal intimate life, beliefs, family or other private attributes."},
    "reputation": {"label": "Potentially uncomfortable aside", "recommend": "remove", "why": "Self-criticism, blame or frustration may not belong in a shared work story; this is your choice, not a judgment."},
    "confidential": {"label": "Third-party / business context", "recommend": "remove", "why": "Customer, personnel or confidential business context may not be appropriate for this audience."},
    "inference": {"label": "Cross-context disclosure risk", "recommend": "remove", "why": "Several details together may reveal more than each detail alone."},
    "custom": {"label": "Your additional redaction", "recommend": "remove", "why": "You selected this exact text for review."},
}

PATTERNS = [
    ("identifier", re.compile(r"(?<![\w.+-])[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)),
    ("identifier", re.compile(r"(?<!\d)(?:\+\d{1,3}[ .-]?)?\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}(?!\d)")),
    ("identifier", re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)")),
    ("identifier", re.compile(r"(?i)(?<=my name is )[A-Z][a-z]+(?: [A-Z][a-z]+){1,3}")),
    ("identifier", re.compile(r"(?i)(?:\b(?:date of birth|born on|passport number|national id|身份证号|出生日期)\s*[:：=]?\s*)([^\s,;。]{4,40})")),
    ("environment", re.compile(r"(?i)(?:[A-Z]:[\\/]Users[\\/]|/(?:Users|home)/)[^\\/\s\"'<>]+")),
    ("environment", re.compile(r"\b(?:10(?:\.\d{1,3}){3}|192\.168(?:\.\d{1,3}){2}|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2})\b")),
    ("environment", re.compile(r"\b(?:[a-z0-9-]+\.)+(?:internal|corp|local|lan)\b", re.I)),
]

PERSONAL_CUE = re.compile(r"\b(?:I|I'm|I've|my|me|my wife|my husband|my partner|our customer|my colleague)\b|我|本人|私[はの]|自分", re.I)
SENTENCES = re.compile(r".+?(?:[;。！？；\n]|[.!?](?=\s|$)|$)")
TECHNICAL_BOUNDARY = re.compile(
    r"[,，]?\s+(?:but|and|so|however)\s+(?=(?:please\s+)?(?:keep|preserve|retain|do not|don't|never|fix|test|verify|ensure|leave|avoid)\b)"
    r"|[,，](?=\s*(?:please\s+)?(?:keep|preserve|retain|do not|don't|never|fix|test|verify|ensure|leave|avoid)\b)"
    r"|[，,](?:但|不过|所以|而且)?(?=请|保留|不要|禁止|确保|修复|验证)", re.I)
CONTEXT_RULES = {
    "health": re.compile(r"\b(?:diagnos\w*|chemotherapy|therapist|antidepressant\w*|bipolar|HIV|cancer|pregnan\w*|miscarriage|medical appointment|psychiatr\w*|oncology|fertility clinic)\b|确诊|抑郁症|化疗|流产|精神科|癌症|妊娠|不妊|通院", re.I),
    "financial": re.compile(r"\b(?:salary|annual income|personal debt|bank balance|credit card debt|bankrupt\w*|mortgage arrears)\b|工资|个人负债|银行卡余额|破产|借金|年収", re.I),
    "personal_life": re.compile(r"\b(?:divorc\w*|affair|sexual orientation|sexuality|religion|religious belief|immigration status|undocumented|gay|lesbian|transgender)\b|离婚|婚外情|性取向|宗教信仰|移民身份|出轨|離婚|性的指向", re.I),
    "reputation": re.compile(r"\b(?:I'm (?:an idiot|stupid|incompetent)|I am (?:an idiot|stupid|incompetent)|embarrass\w*|ashamed|don't tell (?:my|the) (?:boss|manager)|do not tell (?:my|the) (?:boss|manager)|(?:my|the) (?:boss|manager|coworker|colleague) (?:is|was) (?:an idiot|incompetent|useless)|I (?:hate|despise) my (?:job|boss|team)|I have no idea what I'm doing)\b|我太蠢|我真笨|丢脸|羞耻|别告诉.{0,8}(?:老板|经理)|老板.{0,6}(?:蠢|无能)|恥ずかしい|上司に.{0,8}言わない", re.I),
    "confidential": re.compile(r"\b(?:confidential (?:customer|client|project|deal)|unannounced (?:acquisition|layoff|product)|(?:customer|client) (?:secret|private) data|performance improvement plan|HR investigation|do not share outside)\b|客户机密|保密项目|尚未公布|绩效改进|人事调查|社外秘", re.I),
}


def private_clause_end(text, start, end):
    boundary = TECHNICAL_BOUNDARY.search(text, start, end)
    return boundary.start() if boundary else end


def luhn_valid(value):
    digits = [int(character) for character in value if character.isdigit()]
    if not 13 <= len(digits) <= 19 or len(set(digits)) == 1:
        return False
    total = 0
    for index, digit in enumerate(reversed(digits)):
        changed = digit * 2 if index % 2 else digit
        total += changed - 9 if changed > 9 else changed
    return total % 10 == 0


def detect(text):
    found = []
    for category, pattern in PATTERNS:
        for match in pattern.finditer(text):
            start, end = match.span(match.lastindex or 0)
            found.append((start, end, category))
    for match in re.finditer(r"(?<![\w])(?:\d[ -]?){12,18}\d(?![\w])", text):
        if luhn_valid(match.group()):
            found.append((*match.span(), "identifier"))
    for sentence in SENTENCES.finditer(text):
        value = sentence.group()
        for category, pattern in CONTEXT_RULES.items():
            if pattern.search(value) and (PERSONAL_CUE.search(value) or category in {"reputation", "confidential"}):
                if category == "reputation":
                    found.extend((sentence.start() + match.start(), sentence.start() + match.end(), category) for match in pattern.finditer(value))
                else:
                    start = sentence.start() + len(value) - len(value.lstrip())
                    end = private_clause_end(text, start, sentence.end())
                    if pattern.search(text[start:end]):
                        found.append((start, end, category))
    return found
