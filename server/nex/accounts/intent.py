"""Resolve explicit questions about connected accounts without browsing."""

import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime, time, timedelta

from ..recall_dates import recall_dates, time_zone
from ..store import normalized

GMAIL = re.compile(r"\b(?:gmail|inbox|my (?:unread |recent |important )?(?:e-?mails?|mail)|"
                   r"(?:search|find|read|show(?: me)?|list|check|summari[sz]e|send|draft) "
                   r"(?:an? |the |my )?(?:unread |recent |important )?e-?mails?)\b")
CALENDAR = re.compile(r"\b(?:google calendar|my (?:calendar|agenda|meetings?|appointments?|schedule)|"
                      r"(?:on|in) (?:the |my )?calendar|calendar (?:events?|appointments?)|what(?: s| is) on today)\b")
TICKTICK = re.compile(r"\b(?:tick ?tick|my (?:tasks?|to[- ]?dos?|task lists?)|what (?:tasks? |do i have )?to do|"
                      r"(?:show|list|complete|add) (?:the |my |a )?tasks?)\b")
TICKTICK_ACCESS = re.compile(
    r"(?:(?:(?:can|could) (?:youa?|nex) (?:access|read)|do you have access to)"
    r" (?:my )?tick ?tick(?: account| tasks?| lists?)?|is (?:my )?tick ?tick(?: account)? connected)[?.!]*"
)
DAILY_AGENDA = re.compile(
    r"\b(?:what (?:(?:do i (?:have|need)|i (?:have|need)) to do|(?:have i got|do i have)(?: planned)?) "
    r"(?:for )?(?:today|tomorrow)|what(?: s| is) (?:on my plate|on my agenda|my plan) "
    r"(?:for )?(?:today|tomorrow)|(?:today|tomorrow) s agenda|(?:bugun|yarin) ne yapmam gerek\w*)\b"
)
ACTION = re.compile(
    r"^(?:(?:please|nex|hey nex)\s*[,.:]?\s+|(?:can|could|would|will) you\s+)*"
    r"(?:send|reply|forward|draft|archive|trash|delete|move|modify|edit|update|cancel|reschedule|\bmark|"
    r"book|create|add|complete|finish|check off|invite|accept|decline|unsubscribe|rename|post)\b"
)
SEARCH = re.compile(
    r"\b(?:search|find|look for|look up)\b.*?\b(?:for|about|from|containing)\s+(.+)$",
    re.IGNORECASE,
)
READ = re.compile(r"\b(?:my|what|which|when|read|find|search|show|list|pull|check|look up|"
                  r"summari[sz]e|brief|recap|inbox|agenda|today|tomorrow|unread|upcoming|due|overdue)\b")
CONCEPT = re.compile(r"^(?:how (?:do|does|can|to)\b|what (?:is|s) (?:gmail|google calendar|tick ?tick)\s*[?.!]?$)")
MAIL_REFERENCE = re.compile(r"\b(?:gmail(?: inbox| messages?)?|inbox|e-?mails?|mail)\b", re.IGNORECASE)
MAIL_TOPIC = re.compile(r"\s+(?:for|about|from|containing|regarding|related to|on)\s+([^?!;]+)", re.IGNORECASE)
MAIL_FOLLOWUP = re.compile(
    r"\s*(?:(?:no|and|okay|now|just|only|please)\b[\s,]+)*"
    r"(?:(?:show me|show|list|find|check|summari[sz]e)\s+)?(?:only\s+|just\s+)?"
    r"(?:the\s+)?(?:(?P<unread>unread)\s+)?(?:ones?|those|them|results|messages)"
    r"(?P<topic>\s+(?:about|from|containing|regarding|related to|on)\s+[^?!;]+)?[\s?!.]*", re.IGNORECASE,
)
MAIL_PRIORITY = re.compile(r"\b(?:important|urgent|prioriti[sz]e|(?:need|needs|requires?) (?:my )?attention)\b")


@dataclass(frozen=True)
class AccountIntent:
    provider: str
    blocked: bool
    topic: str = ""
    unread: bool = False
    lists: bool = False
    summary: bool = False
    access_check: bool = False
    priority: bool = False
    include_body: bool = False


def search_topic(text):
    return " ".join(re.findall(r"[^\W_]+(?:[-'][^\W_]+)*", text)[:12])[:110]


def mail_followup(query, previous):
    if not previous or previous.blocked or not (match := MAIL_FOLLOWUP.fullmatch(query)):
        return None
    topic = MAIL_TOPIC.match(match["topic"]) if match["topic"] else None
    return replace(previous, topic=search_topic(topic[1]) if topic else previous.topic,
                   unread=previous.unread or bool(match["unread"]),
                   summary=bool(re.search(r"summari[sz]e", query, re.IGNORECASE)), priority=False)


def account_intent(query):
    clean = normalized(query.strip()).replace("'", " ").replace("’", " ")
    if GMAIL.search(clean):
        provider = "gmail"
    elif DAILY_AGENDA.search(clean) and not re.search(r"\b(?:tick ?tick|calendar)\b", clean):
        return AccountIntent("agenda", blocked=False)
    elif CALENDAR.search(clean):
        provider = "calendar"
    elif TICKTICK.search(clean):
        provider = "ticktick"
        if TICKTICK_ACCESS.fullmatch(clean):
            return AccountIntent(provider, blocked=False, access_check=True)
    else:
        return None
    blocked = bool(ACTION.search(clean))
    if not blocked and (not READ.search(clean) or CONCEPT.search(clean)):
        return None
    topic = ""
    priority = False
    search = SEARCH.search(query)
    if provider == "gmail":
        reference = MAIL_REFERENCE.search(query)
        if reference and (match := MAIL_TOPIC.match(query[reference.end():])):
            topic = search_topic(match[1])
        elif search:
            words = re.split(r"\s+in (?:my )?(?:gmail|inbox)\b", search[1], maxsplit=1, flags=re.IGNORECASE)[0]
            topic = search_topic(words)
        priority = bool(MAIL_PRIORITY.search(clean)) and not topic
    elif search:
        topic = search_topic(search[1])
    return AccountIntent(provider, blocked, topic, bool(re.search(r"\bunread\b", clean)),
                         bool(re.search(r"\b(?:lists|projects)\b", clean)),
                         priority or bool(re.search(r"\b(?:summari[sz]e|brief|recap|summary)\b", clean)),
                         priority=priority)


def date_window(query, zone, reference=None):
    tz = time_zone(zone)
    current = (reference or datetime.now(UTC)).astimezone(tz)
    if re.search(r"\b(?:tomorrow|yarin)\b", normalized(query)):
        start = datetime.combine(current.date() + timedelta(days=1), time(), tz)
        return start, datetime.combine(start.date() + timedelta(days=1), time(), tz)
    if scope := recall_dates(query, zone, current):
        start, end = (datetime.fromisoformat(stamp).astimezone(tz) for stamp in scope.bounds)
        return start, min(end, datetime.combine(start.date() + timedelta(days=13), time(), tz))
    days = 1 if re.search(r"\b(?:today|bugun)\b", normalized(query)) else 7
    start = datetime.combine(current.date(), time(), tz)
    return start, datetime.combine(start.date() + timedelta(days=days), time(), tz)
