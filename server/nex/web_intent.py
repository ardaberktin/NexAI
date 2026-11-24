"""Recognize an explicit public lookup and produce only its public terms or URL."""

import re
from dataclasses import dataclass

from .wake import LEADING_WAKE
from .web_transport import WebReadError, public_url

POLITE = r"(?:(?:can|could|would) you\s+|please\s+)?"
ADDRESS = r"(?:https?://|www\.|(?:[a-z0-9-]+\.)+[a-z]{2,63}(?=[:/?#\s.,;!\)]|$))"
SEARCH = re.compile(
    rf"^{POLITE}(?:(?:search|look) (?:(?:the )?web|online|the internet)(?: for)?|web search(?: for)?|"
    r"look up|search for|find online|web'de ara|internette ara)\b[\s,:-]*", re.IGNORECASE,
)
OPEN = re.compile(rf"^{POLITE}(?:open|read|visit|check|go to|summari[sz]e)\s+(?:the (?:website|page)\s+)?"
                  rf"(?={ADDRESS})", re.IGNORECASE)
URL = re.compile(rf"^{ADDRESS}[^\s<>\"']*", re.IGNORECASE)
ARCHIVE = re.compile(r"^(?:(?:my|our|the) )?(?:archive|daybook|journal|recordings?|notes|transcripts?)\b", re.IGNORECASE)
PRIVATE = re.compile(
    r"\b(?:my|mine|me|our|ours|myself|ourselves|here|passwords?|passphrase|credentials?|secrets?|"
    r"access token|api key|private key|recovery phrase|social security|personal (?:data|information)|"
    r"credit card|bank account|medical records?|exact address|street address|phone number|email address|"
    r"benim|bana|adresim|parolam|konumum|yakınımda)\b|[@=]|"
    r"\b(?:\d[ .()-]*){7,}\b|\b\d{1,3}(?:\.\d{1,3}){3}\b|"
    r"\b(?:i (?:am|live|work)|i['’]m|name is|born on)\b|"
    r"\b\d{1,6}\s+(?:[\w'-]+\s+){1,5}(?:street|st|avenue|ave|road|rd|drive|dr|lane|ln|boulevard|blvd)\b",
    re.IGNORECASE,
)
WORDS = re.compile(r"[^\W_]+(?:-[^\W_]+)*")


@dataclass(frozen=True)
class WebRequest:
    url: str | None = None
    terms: str | None = None


def request_text(text):
    clean = LEADING_WAKE.sub("", text.strip(), count=1)
    if match := SEARCH.match(clean):
        target = clean[match.end():].strip()
        if ARCHIVE.match(target) and re.search(r"(?:look up|search for)$", match[0].strip(), re.IGNORECASE):
            return None
        return target
    if match := OPEN.match(clean):
        return clean[match.end():].strip()
    return clean if URL.match(clean) else None


def is_web_request(text):
    return request_text(text) is not None


def web_request(text):
    target = request_text(text)
    if target is None:
        return None
    if match := URL.match(target):
        value = match[0].rstrip(".,;!?)]")
        if value.lower().startswith("http://"):
            value = "https://" + value[7:]
        elif not value.lower().startswith("https://"):
            value = "https://" + value
        return WebRequest(url=public_url(value))
    if PRIVATE.search(target):
        raise WebReadError("I kept that off the web. Ask using a public topic without personal or account details.")
    terms = " ".join(WORDS.findall(target)[:13])
    if not terms or len(terms) > 140 or re.search(r"[/\\]", target):
        raise WebReadError("Give me a short public search topic or a public HTTPS address.")
    return WebRequest(terms=terms)
