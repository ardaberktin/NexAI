"""Answer a spoken time-budget question when both required durations are explicit."""

import re

DURATION = r"(\d{1,3})\s+(minutes?|hours?)\b"
WINDOW = re.compile(r"(?:\b(?:have|got)\s+" + DURATION + r"\s+(?:before|until|left)\b|"
                    r"\b(?:starts?|leaves?|departs?|begins?)\s+in\s+" + DURATION + r")", re.IGNORECASE)
COST = re.compile(r"\b(?:takes?|needs?)\s+(?:me\s+)?" + DURATION, re.IGNORECASE)
QUESTION = re.compile(r"\bhow (?:long|much time|many minutes) (?:can i wait|do i have (?:left|to spare))\b", re.IGNORECASE)
UNCERTAIN = re.compile(r"\b(?:about|around|roughly|between|at least|at most|not|never|\w+n['’]t)\b", re.IGNORECASE)


def time_budget(query):
    if not QUESTION.search(query) or len(re.findall(DURATION, query, re.IGNORECASE)) != 2:
        return None
    window, cost = WINDOW.search(query), COST.search(query)
    if not window or not cost or UNCERTAIN.search(query):
        return None

    def minutes(value, unit):
        return int(value) * (60 if unit.lower().startswith("hour") else 1)

    amount, unit = window.groups()[:2] if window[1] else window.groups()[2:]
    return minutes(amount, unit) - minutes(*cost.groups())


def budget_reply(remaining, language):
    count = abs(remaining)
    unit = "minute" if count == 1 else "minutes"
    if remaining < 0:
        text = f"You’re already {count} {unit} short, even if you leave now."
    elif remaining == 0:
        text = "You need to leave now to arrive on time."
    else:
        text = f"You can wait up to {count} {unit} before leaving."
    return {"text": text, "language": language, "engine": "calculation", "sources": []}
