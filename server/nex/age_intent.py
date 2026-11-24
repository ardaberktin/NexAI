"""Resolve personal age questions and unit-only follow-ups against the current exchange."""

import re
from dataclasses import dataclass

from .birthdate import CLOCK, REMEMBER, BirthDate, birth_date
from .conversation_intent import words
from .wake import spoken_wake_body

UNIT = r"(?:year|month|week|day|hour|minute|second)s?"
UNIT_LIST = rf"{UNIT}(?: (?:and |or )?{UNIT})*"
UNITS = re.compile(rf"\b({UNIT})\b")
PREFIX = re.compile(r"^(?:(?:can|could|would) you |please |tell me |and )+")
AGE = re.compile(rf"(?:(?:how old (?:am i|i am)|(?:what(?: s| is)|calculate) my (?:exact )?age|my age)"
                 rf"(?: (?:today|now|right now))?(?: in {UNIT_LIST})?|"
                 rf"how many {UNIT_LIST} (?:old am i|am i|have i (?:lived|been alive)))")
FOLLOWUP = re.compile(rf"(?:(?:what|how) about |how many |what(?: s| is) that in )?"
                      rf"(?:in )?{UNIT_LIST}(?: (?:is that|is it|in total|total))?")
BIRTH_CLAUSE = re.compile(r"[\s,.?!]+(?:(?:if|since|given that) )?(?=i was born |my (?:birth|date of birth|dob))",
                         re.IGNORECASE)
AGE_CLAUSE = re.compile(r"[\s,.:!?]+(?=(?:how old|how many|what['’]s my age|what is my age)\b)", re.IGNORECASE)
ACKNOWLEDGMENT = re.compile(r"(?:thanks|thank you|ok(?:ay)?|yes(?: please)?|got it|nope|no thanks|what does that mean)")


@dataclass(frozen=True)
class AgeRequest:
    units: tuple[str, ...]
    birth: BirthDate | None = None
    clock: re.Match | None = None


def wording(text):
    value = words(spoken_wake_body(text) or text).removesuffix(" please")
    return PREFIX.sub("", value, count=1)


def units_in(value):
    return tuple(dict.fromkeys(unit.removesuffix("s") for unit in UNITS.findall(value)))


def duration_followup(text):
    value = wording(text)
    return units_in(value) if FOLLOWUP.fullmatch(value) else ()


def direct_age(text):
    question = text
    birth = None
    if split := BIRTH_CLAUSE.search(text):
        question, date_text = text[:split.start()], text[split.end():]
        if not (birth := birth_date(date_text)):
            return None
    elif split := AGE_CLAUSE.search(text):
        date_text, question = text[:split.start()], text[split.end():]
        if not (birth := birth_date(date_text)):
            question = text
    value = wording(question)
    return AgeRequest(units_in(value) or ("year",), birth) if AGE.fullmatch(value) else None


def prior_age(history):
    """Only carry details through consecutive age turns, including pre-calculator chats."""
    units, birth, clock = (), None, None
    for i in range(len(history) - 1, 0, -2):
        question, answer = history[i - 1:i + 1]
        if question["kind"] != "question" or answer["kind"] != "answer":
            break
        text = question["text"]
        if direct := direct_age(text):
            return AgeRequest(units or direct.units, birth or direct.birth, clock)
        if stated := birth_date(text):
            birth = birth or stated
            if answer.get("engine") != "calculation":
                return AgeRequest(units or ("year",), birth, clock)
        elif ACKNOWLEDGMENT.fullmatch(words(text)):
            continue
        elif followup := duration_followup(text):
            units = units or followup
        elif answer.get("engine") == "calculation" and (given := birth_date(text, bare=True)):
            birth = birth or given
        elif answer.get("engine") == "calculation" and (given_time := CLOCK.fullmatch(text.strip(" .,!?"))):
            clock = clock or given_time
        else:
            break
    return None


def age_request(query, history):
    text = (spoken_wake_body(query) or query).strip()
    if REMEMBER.match(text):
        return None
    if direct := direct_age(text):
        return direct
    units = duration_followup(text)
    birth = birth_date(text, bare=True)
    clock = CLOCK.fullmatch(text.strip(" .,!?"))
    if (units or birth or clock) and (previous := prior_age(history)):
        if birth:
            return AgeRequest(previous.units, birth)
        return AgeRequest(units or previous.units, previous.birth, clock or previous.clock)
    return None
