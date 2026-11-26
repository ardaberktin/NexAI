"""Select relevant page text, including product questions answered directly by a support page."""

import re
from urllib.parse import urlsplit

from .passages import matching_passage
from .store import normalized

# These suffixes are the public search terms produced for product availability and release questions.
PRODUCT_QUERY = re.compile(r"^(.+?) (platforms|release date|where to watch|streaming availability|where to buy)"
                           r"(?: on .+)?$", re.IGNORECASE)
HEADINGS = {
    "platforms": r"^(?:(?:on|for) )?(?:what|which) (?:gaming )?(?:platforms?|consoles?|systems?)\b",
    "release date": r"\b(?:release date|when .*(?:launch|release|coming out))\b",
    "where to watch": r"\bwhere .*(?:watch|stream)\b",
    "streaming availability": r"\bwhere .*(?:watch|stream)\b",
    "where to buy": r"\bwhere .*(?:buy|order|purchase)\b",
}
ROMAN = {str(i): numeral for i, numeral in enumerate(
    ("I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X", "XI", "XII"), 1)}


def mentions_product(subject, text):
    words = re.findall(r"[^\W_]+", subject)
    reference = normalized(text)
    for word in words:
        forms = [re.escape(normalized(word))]
        if word in ROMAN:
            forms.append(ROMAN[word].lower())
        if word.isalpha() and 2 <= len(word) <= 5:
            # Search may use initials where the publisher spells out a product name.
            forms.append(r"\w*\s+".join(re.escape(c) for c in normalized(word)) + r"\w*")
        if not re.search(r"\b(?:" + "|".join(forms) + r")\b", reference):
            return False
    return bool(words)


def product_answer(page, query):
    match = PRODUCT_QUERY.fullmatch(query)
    if not match:
        return None
    lines = [line.strip() for line in page.text.splitlines() if line.strip()]
    heading_pattern = HEADINGS[match[2].lower()]
    for i, heading in enumerate(lines[:-1]):
        if not heading.endswith("?") or len(heading) > 180 or not re.search(heading_pattern, heading, re.IGNORECASE):
            continue
        generic = re.fullmatch(r"(?:what|which) (?:platforms?|consoles?|systems?) "
                               r"(?:are supported|does it support|is it available on)\?", heading, re.IGNORECASE)
        if not mentions_product(match[1], heading) and not (generic and mentions_product(match[1], page.title)):
            continue
        answer = lines[i + 1]
        if 12 <= len(answer) <= 340 and answer.endswith((".", "!")) and "?" not in answer:
            return heading, answer
    return None


def page_evidence(number, page, topic):
    result = {"number": number, "title": page.title, "url": page.url}
    if pair := product_answer(page, topic):
        heading, answer = pair
        result["excerpt"] = heading + "\n" + answer
        url = urlsplit(page.url)
        if (url.hostname or "").split(".")[0] in {"support", "help", "docs"} or "/support/" in url.path:
            result["source_answer"] = answer
    else:
        result["excerpt"] = matching_passage(page.text, topic, limit=1250)
    return result


def direct_answer(evidence, language):
    if language != "en":
        return None
    answers = {page["source_answer"]: page["number"] for page in evidence or () if page.get("source_answer")}
    if len(answers) == 1:
        answer, number = next(iter(answers.items()))
        return f"{answer} [{number}]"
    return None
