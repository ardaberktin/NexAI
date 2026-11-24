"""Recognize brief social turns and requests for the recording archive."""

import re

from .store import normalized
from .wake import LEADING_WAKE

GREETING = r"(?:hello|hi|hey|merhaba|selam)"
SCREEN_REFERENCE = re.compile(r"\b(?:(?:on|from) (?:my |the |this |our |your |the shared )?screen|my screen|"
                              r"(?:app(?:lication)?|tab|window|dialog|button) (?:is |was )?"
                              r"(?:open|active|selected|visible)|(?:active|selected|shared|visible) (?:window|tab|text)|"
                              r"the (?:error message|selected text|active window))\b", re.IGNORECASE)
LEADING_GREETING = re.compile(rf"^{GREETING}(?: nex)? ")
CLIPPED_GREETING = re.compile(rf"^({GREETING}) (?:how(?: are)?|nasil)$")
GREET = re.compile(rf"^(?:(?:no )?i said |hayir )?{GREETING}(?: there| nex)?(?: dedim)?$")
CHECK_IN = re.compile(rf"^(?:(?:no )?i said )?(?:{GREETING}(?: nex)? )?"
                      r"(?:how are you(?: doing)?(?: today)?|how s it going|nasilsin|nasilsiniz)(?: nex)?$")
ARCHIVE_REQUEST = re.compile(
    r"\b(?:(?:my|our|the|nex) (?:archive|journal|recordings?|transcripts?|notes)|"
    r"arsiv\w*|kayitlar\w*|gunlug\w*|notlarim\w*|what do you (?:know|remember) about me|"
    r"do you remember (?:when|what|where)|remember what i said)\b"
)
PAST_QUESTION = re.compile(
    r"^(?:(?:what|where|when|why|how|which|who) .*\bdid (?:i|we)\b|did (?:i|we)\b|"
    r"(?:what|where|when|why|how|which|who) (?:did|was|were)\b.*\bour\b)|"
    r"\b(?:ne|nerede|ne zaman|kim|hangisi|hangi|nasil)\b.*\b"
    r"(?:konustu\w*|anlas\w*|karar verd\w*|soyledi\w*|dedik|biraktik|koyduk)\b|"
    r"\b(?:konustuk|anlastik|karar verdik|soyledik|dedik)\s+(?:mu|mi|ne|nerede|ne zaman)\b"
)
PAST_PERIOD = re.compile(r"\b(?:happened|did you say|talked about|discussed|konustuk|oldu|dedin)\b")
HISTORY_SUFFIX = r"(?: (?:just now|before(?: this)?|previously|earlier|recently|last))?"
QUESTION_HISTORY = re.compile(
    r"^(?:(?:remind me|do you remember) )?(?:"
    rf"what (?:did i (?:just )?ask|have i asked|had i asked)(?: you)?{HISTORY_SUFFIX}|"
    r"what (?:were|are) my (?:last|previous|recent) questions|"
    r"(?:sana )?(?:(?:daha )?once |az once |son olarak )?ne(?:ler)? sordum(?: sana)?|"
    r"son sorularim neydi)$"
)
LAST_QUESTION = re.compile(
    r"^(?:(?:remind me|do you remember) )?(?:what (?:was|is) (?:my (?:last|previous) question|"
    r"the last (?:question|thing) i asked(?: you)?)|(?:sana )?son sorum neydi)$"
)
LAST_HINT = re.compile(r"\b(?:just now|last)$|\b(?:just ask|son olarak|az once)\b")
EARLIER = re.compile(
    r"(?:(?:no|yeah|yes|okay|ok|well|wait|please|and|but|what|i meant|i mean|go back|"
    r"what about|how about|the question|the one|one|what did i ask(?: you)?) )*"
    r"(?:before (?:that|it|that one)|one (?:further )?back|the previous one|the one before)|"
    r"(?:hayir )?(?:ondan once(?:ki)?|bir onceki)(?: sorum)?"
)
NOISY_EARLIER = re.compile(r"[a-z]{2,14} before that")
POSSIBLE_CORRECTION = re.compile(r"(?:i|what|no|yeah|and) [a-z]{1,16} before that")
RECENT_DISCUSSION = re.compile(
    r"\b(?:what (?:were we|we were|are we) (?:just )?(?:talking about|discussing)|"
    r"what did we (?:just )?(?:discuss|talk about)(?: just now)?$|what have we been (?:discussing|talking about)|"
    r"what did we (?:just )?(?:decide(?: on)?|agree on|settle on)$|"
    r"what (?:did (?:you|i) just (?:say|tell (?:you|me))|were you (?:just )?saying)|"
    r"(?:recap|summari[sz]e) (?:this|our) (?:chat|conversation))\b"
)
FOLLOWUP = re.compile(r"\b(?:it|that|those|them|instead|the first one|the other one)\b|"
                      r"^(?:why|how so|go on|tell me more|say more|keep going|and then)\b")


def words(text):
    return " ".join(re.findall(r"[^\W_]+", normalized(text)))


def greeting_reply(text, preference):
    clean = words(text)
    check_in, greeting = CHECK_IN.fullmatch(clean), GREET.fullmatch(clean)
    if not (check_in or greeting):
        return None
    turkish = any(word in clean.split() for word in ("merhaba", "selam", "nasilsin", "nasilsiniz"))
    language = preference if preference != "auto" else ("tr" if turkish else "en")
    if check_in:
        return ("İyiyim, sen nasılsın?", "tr") if language == "tr" else ("I’m good. How are you?", "en")
    return ("Merhaba!", "tr") if language == "tr" else ("Hey!", "en")


def wants_archive(text, scope):
    clean = LEADING_GREETING.sub("", words(text), count=1)
    return bool(ARCHIVE_REQUEST.search(clean) or PAST_QUESTION.search(clean)
                or (scope and (not scope.query or PAST_PERIOD.search(clean))))


def prior_question_limit(text):
    clean = LEADING_GREETING.sub("", words(LEADING_WAKE.sub("", text.strip(), count=1)), count=1)
    if LAST_QUESTION.fullmatch(clean):
        return 1
    if QUESTION_HISTORY.fullmatch(clean):
        return 1 if LAST_HINT.search(clean) else 4
    return 0


def earlier_question(text, *, allow_noisy=False):
    clean = words(LEADING_WAKE.sub("", text.strip(), count=1))
    return bool(EARLIER.fullmatch(clean) or (allow_noisy and NOISY_EARLIER.fullmatch(clean)))


def check_earlier_audio(text):
    clean = words(text)
    return not EARLIER.fullmatch(clean) and bool(NOISY_EARLIER.fullmatch(clean) or POSSIBLE_CORRECTION.fullmatch(clean))


def discussion_request(text):
    return bool(RECENT_DISCUSSION.search(words(text)))


def conversational_followup(text):
    return bool(FOLLOWUP.search(words(text)) or earlier_question(text) or discussion_request(text))
