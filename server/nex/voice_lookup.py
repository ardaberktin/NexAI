"""Resolve public lookups and their follow-ups across typed and spoken conversations."""

import re
from dataclasses import dataclass
from itertools import pairwise

from .conversation_history import MAX_EXCHANGES
from .conversation_intent import words
from .lookup_topics import (
    RELEASE,
    current_topic,
    local_question,
    numbered_title,
    public_question,
    public_topic,
)
from .wake import spoken_wake_body
from .web_intent import is_web_request, web_request
from .web_transport import WebReadError

SEARCH = "Search the web for "
RETRY = re.compile(r"(?:please )?(?:look (?:it|that) up(?: yourself)?|(?:double[- ]?check|verify|check) "
                   r"(?:it|that)(?: online)?|search (?:the web|online))(?: please)?", re.IGNORECASE)
YES = re.compile(r"(?:yes|yeah|sure|okay|ok|evet)(?:[, ]+(?:please|go ahead|do that|lütfen))?", re.IGNORECASE)
OFFER = re.compile(r"\b(?:check (?:a |the )?(?:reliable |official )?source|look (?:it|that) up|search (?:online|the web))\b", re.IGNORECASE)
REPEAT = re.compile(r"\b(?:(?:can|could|would) you )?(?:please )?"
                    r"(?:repeat (?:that|it)(?: again)?|say (?:that|it) again)$", re.IGNORECASE)
CORRECTION = re.compile(r"^(?:no|oh|sorry|actually|i mean|i meant)\b[ ,.!:-]+(.+)$", re.IGNORECASE)
CHALLENGE = re.compile(r"^(?:(?:no|actually)[ ,.!:-]+)?(?:are you (?:sure|certain)|(?:that(?:['’]s| is)|"
                       r"you(?:['’]re| are)) (?:wrong|incorrect|mistaken|not (?:right|correct|true))|"
                       r"(?:it(?:['’]s| is)|you) (?:only|not|can['’]?t|cannot)|that doesn['’]?t sound right|"
                       r"i (?:don['’]?t think|disagree)|really$)\b", re.IGNORECASE)
RELEASE_FOLLOWUP = re.compile(r"(?:when (?:is it coming out|will it (?:launch|be released))|is it out yet|"
                              r"how (?:long|many days)(?: is that)? (?:until then|away)|how many days is that)", re.IGNORECASE)
PLATFORM_FOLLOWUP = re.compile(r"(?:where (?:can|do) (?:i|we|you) play (?:it|that)|"
                               r"(?:which|what) platforms? (?:is (?:it|that game) (?:available )?on|will it (?:support|be on)))", re.IGNORECASE)
PRICE_FOLLOWUP = re.compile(r"(?:how much (?:is (?:it|that)|does (?:it|that) cost)|what(?:['’]s| is) "
                            r"(?:its|the) (?:current )?price|(?:and |what about )(?:the )?price)", re.IGNORECASE)
MEDIA_FOLLOWUP = re.compile(r"where (?:can|do) (?:i|we|you) (watch|stream|buy|order) (?:it|that)", re.IGNORECASE)
FACET = re.compile(r"(?:and )?(?:(?:what|how) about |(?:is it (?:also |available |out )?|"
                   r"does it (?:also )?(?:work|run) |can (?:i|we|you) (?:play|watch|stream|run) it )?on )(.{1,55})", re.IGNORECASE)
ASPECTS = ("release date", "platforms", "price", "where to watch", "where to buy", "streaming availability")


@dataclass(frozen=True)
class Lookup:
    command: str
    original: str | None = None


def refine(previous, detail):
    if not previous:
        return None
    original = previous.original or previous.command
    request = web_request(original)
    if request.url:
        return previous
    base = re.sub(r" platforms on .+$", " platforms", request.terms, flags=re.IGNORECASE)
    if detail in ASPECTS:
        for aspect in ASPECTS:
            if base.endswith(" " + aspect):
                base = base.removesuffix(" " + aspect)
                break
    elif base.endswith(" platforms"):
        detail = "on " + detail
    return Lookup(SEARCH + base + " " + detail, original)


def repeated_topic(anchor, topics):
    if not anchor:
        return topics[-1][0] if topics else None
    anchor = re.sub(r"^(?:sir|ser|so|about|hey)[ ,]+", "", anchor, flags=re.IGNORECASE)
    if not public_topic(anchor):
        return None
    phrase = " " + words(numbered_title(anchor)) + " "
    for topic, _ in reversed(topics):
        if topic and phrase in " " + words(topic.command.removeprefix(SEARCH)) + " ":
            return topic
    return None


def resolve_turn(text, topics):
    value = text.strip(" .?!,")
    previous, answer = topics[-1] if topics else (None, "")
    if RETRY.fullmatch(value):
        return previous or Lookup(SEARCH.rstrip())
    if is_web_request(text):
        return Lookup(text)
    if local_question(text):
        return None
    if repeat := REPEAT.search(value):
        return repeated_topic(value[:repeat.start()].strip(" ,.?!"), topics)
    if YES.fullmatch(value):
        return previous if OFFER.search(answer) else None
    if RELEASE_FOLLOWUP.fullmatch(value):
        return refine(previous, "release date")
    if PLATFORM_FOLLOWUP.fullmatch(value):
        return refine(previous, "platforms")
    if PRICE_FOLLOWUP.fullmatch(value):
        return refine(previous, "price")
    if media := MEDIA_FOLLOWUP.fullmatch(value):
        return refine(previous, "where to watch" if media[1].lower() in {"watch", "stream"} else "where to buy")
    if facet := FACET.fullmatch(value):
        return refine(previous, facet[1]) if public_topic(facet[1]) else None
    if previous and (correction := CORRECTION.match(value)):
        body = correction[1]
        if facet := FACET.fullmatch(body):
            return refine(previous, facet[1]) if public_topic(facet[1]) else None
        if topic := public_question(body):
            return Lookup(SEARCH + topic)
        release = "When does " + body
        if RELEASE.fullmatch(release) and (topic := current_topic(release)):
            return Lookup(SEARCH + topic)
    if CHALLENGE.match(value):
        return previous
    topic = public_question(text)
    return Lookup(SEARCH + topic) if topic else None


def reusable(query):
    """Carry forward only a request the public reader can actually send."""
    try:
        request = web_request(query.command) if query else None
    except WebReadError:
        return None
    if request is None:
        return None
    command = "Read " + request.url if request.url else SEARCH + request.terms
    return Lookup(command, query.original)


def lookup_query(text, history=()):
    # Rebuild the topic from user questions, including archives created before automatic lookup.
    # An answer can confirm that Nex offered a search. Its contents never become search terms.
    topics = []
    recent = history[-2 * MAX_EXCHANGES:]
    for question, answer in pairwise(recent):
        if question["kind"] != "question" or answer["kind"] != "answer":
            continue
        query = None
        if answer.get("engine") in {None, "web_lookup", "local_model"}:
            query = resolve_turn(question["text"], topics)
            if query is None and question.get("web_query"):
                query = Lookup(question["web_query"])
        topics.append((reusable(query), answer["text"]))
    # An interrupted question has no answer to verify or repeat.
    if history and history[-1]["kind"] != "answer":
        topics.append((None, ""))
    result = resolve_turn(text, topics)
    return result.command if result else None


def voice_lookup_query(text, history=()):
    return lookup_query(spoken_wake_body(text) or text, history)
