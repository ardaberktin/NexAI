"""Extract a public search topic from an ordinary factual question."""

import re

from .accounts.intent import account_intent
from .age_intent import duration_followup
from .coding.intent import python_request
from .conversation_intent import (
    SCREEN_REFERENCE,
    discussion_request,
    earlier_question,
    prior_question_limit,
    wants_archive,
)
from .personal_intent import personal_intent
from .recall_dates import recall_dates
from .time_budget import time_budget
from .wake import next_wake_body, spoken_wake_body
from .web_intent import PRIVATE

# Extract the requested topic. Filler such as "find me when" is not a search term.
PREFIX = re.compile(
    r"^(?:(?:hey|please)[ ,]+|(?:can|could|would) you (?:please )?|"
    r"look (?:it|that) up (?:yourself )?and |(?:tell me|find me|find out|check|do you know) "
    r"(?=who|what|when|where|which|why|how|if|whether))*", re.IGNORECASE,
)
RELEASE = re.compile(
    r"^(?:when (?:is |does |will |did |was )?|how (?:long|many days)(?: is (?:there )?left)? "
    r"(?:until|till|before) )(?P<subject>.+?) (?:is |will be |will |was |be )?"
    r"(?:coming out|comes? out|came out|releas(?:e[ds]?|ing)|launch(?:es|ed|ing)?|premier(?:es?|ed|ing))$",
    re.IGNORECASE,
)
RELEASE_DATE = re.compile(
    r"^(?:(?:what(?:['’]?s| is)|when is|give me) (?:the )?)?(?:release|launch|premiere) date (?:of|for) (.+)$|"
    r"^(?:(?:what(?:['’]?s| is)|when is) )?(.+?)(?:['’]s)? (?:release|launch|premiere) date$", re.IGNORECASE,
)
CURRENT = re.compile(
    r"\b(?:latest|current(?:ly)?|upcoming|today(?:['’]s)?|tonight|right now|this (?:week|month|year)|"
    r"still available|out yet|available yet|in stock|news|exchange rate|stock price|share price|standings|"
    r"release date|launch date|ne zaman (?:çıkacak|yayınlanacak)|güncel|son durum)\b|"
    r"\b(?:who (?:is (?:the )?(?:president|prime minister|ceo|champion)|won)|"
    r"how much (?:is|does) .+ (?:worth|cost)|(?:what(?:['’]?s| is)|when is) (?:the )?"
    r"(?:score|price|next (?:match|game|launch|election)))\b", re.IGNORECASE,
)
PRIVATE_CONTEXT = re.compile(
    r"\b(?:i|we|our|us|mine|you|your|yours|they|he|she|screen|daybook|archive|transcripts?|"
    r"inbox|calendar|tasks?|notes?|recordings?|memory|soul|journal|nex|colleague|coworker|client|"
    r"internal|confidential|private|meeting|appointment|project|the (?:encrypted )?backups?|"
    r"ben|biz|bizim|ekran|takvim)\b", re.IGNORECASE,
)
ANAPHORA = re.compile(r"\b(?:it|that|this|those|these|them|there|the (?:other|first|second) one)\b", re.IGNORECASE)
QUESTION = re.compile(r"^(?:who|what|when|where|which|why|how|is|are|was|were|has|have|did|does|do|will|can|"
                      r"whether|if|latest|current|upcoming|news|price|compare|explain|define|"
                      r"ne|kaç|hangi|kim|neden|nasıl)\b", re.IGNORECASE)
FACT_REQUEST = re.compile(
    r"^(?:tell me about|(?:give|show) me (?:\w+ )?(?:facts?|information|details) (?:about|on)) (.+)$|"
    r"^(.+?) hakkında (?:\w+ )?bilgi ver$", re.IGNORECASE,
)
ACCESS = re.compile(
    r"^(?:where (?:can|could|do) (?:i|we|you)|(?:can|could) (?:i|we)|where to) "
    r"(play|watch|stream|buy|get|order|find|download|listen to) (.+)$", re.IGNORECASE,
)
ACCESS_TERMS = {"play": "platforms", "watch": "where to watch", "stream": "streaming availability",
                "buy": "where to buy", "order": "where to buy", "listen to": "where to listen"}
PLATFORMS = re.compile(r"^(?:what|which) (?:platforms?|consoles?|systems?) "
                       r"(?:(?:can (?:i|we|you) play|is|will) (.+?) (?:be |come out |available |launch )?on|"
                       r"support (.+))$", re.IGNORECASE)
HOW_TO = re.compile(r"^how (?:can|could|do) (?:i|we|you) (.+)$", re.IGNORECASE)
PUBLIC_MODAL = re.compile(r"\b(can|could|must|do|should) (?:i|we|you) (?=use|install|run|buy|watch|play|"
                          r"bring|carry|visit|travel|apply|renew|upgrade|need|download)\b", re.IGNORECASE)
SUBJECT_FACT = re.compile(r"\b(?:platforms?|availability|system requirements|specifications|standings|"
                          r"stock price|opening hours|hangi platformlarda|kimdir|nedir)$", re.IGNORECASE)
LOCAL_REQUEST = re.compile(r"^(?:(?:write|draft|compose|draw|imagine|invent|translate|rewrite|roleplay|what if)\b|"
                           r"what(?:['’]s| is) up$|how (?:are things|is it going)\b|(?:what is|how much is|calculate) "
                           r"(?:[\d .+*/×÷()%-]+|.+ (?:plus|minus|times|divided by) .+)$)", re.IGNORECASE)
OFFLINE = re.compile(r"\b(?:(?:don['’]?t|do not|without|no) (?:search(?:ing)?|brows(?:e|ing)|"
                     r"look(?:ing)? (?:it |that )?up|us(?:e|ing) the (?:web|internet))|answer offline)\b", re.IGNORECASE)
COUNTDOWN_TOPIC = re.compile(r"^how (?:long|many days)(?: is (?:there )?left)? (?:until|till|before) (.+)$", re.IGNORECASE)
SERIES_NUMBERS = dict(zip(["two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"], range(2, 11)))
SERIES_NUMBER = re.compile(r"(?<=\w) (" + "|".join(SERIES_NUMBERS) + r")$", re.IGNORECASE)


def public_topic(text):
    """Validate the entire extracted phrase before any keywords leave Nex."""
    return (2 <= len(text) <= 160 and not PRIVATE.search(text) and not PRIVATE_CONTEXT.search(text)
            and not re.search(r"[\n;:/\\]|[.!?]\s+\w|\b(?:and (?:my|our|tell)|because)\b", text, re.IGNORECASE))


def question_wording(text):
    value = (spoken_wake_body(text) or text).strip(" .?!,")
    alternate = next_wake_body(value)
    if alternate and QUESTION.match(alternate):
        value = alternate
    return PREFIX.sub("", value, count=1)


def numbered_title(subject):
    return SERIES_NUMBER.sub(lambda match: " " + str(SERIES_NUMBERS[match[1].lower()]), subject)


def availability_topic(value):
    if platform := PLATFORMS.fullmatch(value):
        subject = platform[1] or platform[2]
        return numbered_title(subject) + " platforms" if public_topic(subject) and not ANAPHORA.search(subject) else None
    match = ACCESS.fullmatch(value)
    if not match:
        return None
    verb, subject = match[1].lower(), match[2]
    if not public_topic(subject) or ANAPHORA.search(subject):
        return None
    if verb == "play" and (device := re.fullmatch(r"(.+?) on (.+)", subject, re.IGNORECASE)):
        return numbered_title(device[1]) + " platforms on " + device[2]
    return numbered_title(subject) + " " + ACCESS_TERMS.get(verb, "where to " + verb)


def current_topic(text):
    """High-confidence changing facts also used by wake detection and speech verification."""
    value = question_wording(text)
    if topic := availability_topic(value):
        return topic
    if not public_topic(value):
        return None
    if release := RELEASE.fullmatch(value):
        subject = release["subject"]
    elif date := RELEASE_DATE.fullmatch(value):
        subject = date[1] or date[2]
    elif countdown := COUNTDOWN_TOPIC.fullmatch(value):
        return value if not ANAPHORA.search(countdown[1]) else None
    else:
        asking = QUESTION.match(value) or text.rstrip().endswith("?") or "ne zaman" in value.casefold()
        subject = re.sub(r"\bthis (?:week|month|year)\b", "", value, flags=re.IGNORECASE)
        return value if asking and CURRENT.search(value) and not ANAPHORA.search(subject) else None
    return None if ANAPHORA.search(subject) else numbered_title(subject) + " release date"


def local_question(text):
    return (OFFLINE.search(text) or SCREEN_REFERENCE.search(text) or account_intent(text) or personal_intent(text)
            or time_budget(text) is not None or python_request(text)
            or prior_question_limit(text) or earlier_question(text, allow_noisy=True)
            or discussion_request(text) or wants_archive(text, recall_dates(text)))


def public_question(text):
    value = question_wording(text)
    if local_question(text) or LOCAL_REQUEST.match(value) or duration_followup(text):
        return None
    if topic := current_topic(text):
        return topic
    if request := FACT_REQUEST.fullmatch(value):
        value = request[1] or request[2]
    elif how := HOW_TO.fullmatch(value):
        value = "how to " + how[1]
    elif not (QUESTION.match(value) or SUBJECT_FACT.search(value)):
        return None
    else:
        value = PUBLIC_MODAL.sub(lambda m: ("does" if m[1].lower() == "do" else m[1]) + " someone ", value, count=1)
    if not public_topic(value) or ANAPHORA.search(value):
        return None
    return value
