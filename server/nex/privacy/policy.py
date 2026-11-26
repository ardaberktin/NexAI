"""Separate private requests from public generation."""

import re

from ..accounts.intent import account_intent
from ..activities.recall import activity_question
from ..age_intent import age_request
from ..conversation_intent import (
    discussion_request,
    earlier_question,
    prior_question_limit,
    wants_archive,
)
from ..daybook import recap_context
from ..goal_requests import goal_request
from ..personal_intent import personal_intent
from ..recall_dates import recall_dates
from ..store import normalized
from ..weather_intent import weather_request

SCREEN = re.compile(r"\b(on (?:my |the |this )?screen|shared (?:screen|window)|what am i looking at|"
                    r"look at (?:my |the )?screen|ekran\w*)\b")
PERSONAL = re.compile(r"\b((?:my|our) (?:name|address|phone|number|birthday|password|location|history|"
                      r"preferences|files?|messages?|records?|plans?|reminders?|accounts?|goals?|family|work|"
                      r"calendar|schedule|appointments?|contacts?|inbox|e-?mails?|home|wife|husband|partner|"
                      r"children|parents?|salary|balance|medical records?|last conversation)|"
                      r"about me|who am i|where (?:do i live|am i)|when was i born|daybook|benim|ben kimim|adim|takvimim\w*|"
                      r"randevu\w*|mesajlarim\w*|hatirla|hatirlat|kisisel|private answer)\b", re.IGNORECASE)


def request_kind(text, zone, *, journal=None, force=False, history=()):
    if journal or goal_request(text):
        return "private"
    if SCREEN.search(normalized(text)):
        return "screen"
    if force:
        return "private"
    weather = weather_request(text)
    if weather and weather.is_local:
        # Only a complete weather question can use the device's approximate location.
        return "public"
    if weather and (weather.remember or weather.forget):
        return "private"
    if (PERSONAL.search(normalized(text)) or account_intent(text) or personal_intent(text)
            or age_request(text, history) or activity_question(text) or recap_context(text, zone)
            or prior_question_limit(text) or earlier_question(text) or discussion_request(text)
            or wants_archive(text, recall_dates(text, zone))):
        return "private"
    return "public"


def messages(text, language):
    return [{"role": "system", "content": (
        "Answer public questions concisely in " + ("Turkish" if language == "tr" else "English")
        + ". You have no access to anyone's accounts, identity, location, screen or memories. "
        "For requests needing personal data, ask the user to request a private answer in Nex."
    )}, {"role": "user", "content": text}]
