"""Recognize live weather requests and city-only answers to a location prompt."""

import re
from dataclasses import dataclass

from .wake import spoken_wake_body
from .web_intent import PRIVATE

QUESTION = re.compile(
    r"^(?:(?:how|what)(?:['’]?s| is) (?:the )?(?:local )?weather(?: like)?|(?:check (?:the )?)?(?:local )?weather)"
    r"(?: (?:outside|out there|today|right now|now|here|nearby|(?:around|near) (?:here|me)|where i am|"
    r"in my area|(?:at|in) my (?:current )?location))*"
    r"(?: (?:in|for) (?P<city>.+?)(?: today| right now| now)?)?$",
    re.IGNORECASE,
)
TURKISH = re.compile(r"^(?:(?P<city>.+?)['’]?(?:\s+|['’])(?:da|de|ta|te)\s+)?(?:bugün |dışarıda )?hava nasıl$", re.IGNORECASE)
REMEMBER = re.compile(r"^use (.+?) for (?:the )?weather$|^set my weather city to (.+?)(?: for (?:the )?weather)?$",
                      re.IGNORECASE)
PLACE = re.compile(r"[^\W\d_]+(?:[ .'’,-]+[^\W\d_]+)*$")
NOT_PLACE = re.compile(
    r"\b(?:what|how|when|where|why|who|should|could|tell|show|forget|remember|"
    r"weather|forecast|tomorrow|yesterday|please|address|passport|secret|i|you|yes|no|"
    r"read|search|explain|cancel|stop|never mind)\b", re.IGNORECASE,
)
MISHEARD_OUTSIDE = re.compile(
    r"^(?:how(?:['’]?s| is)|house) (?:it )?with (?:the |a )?(?:lakot sign|like hot side)[.?!]*$",
    re.IGNORECASE,
)
OUTSIDE_WORDING = re.compile(r"\blike (?:outside|out side)\b", re.IGNORECASE)
MISHEARD_WEATHER = re.compile(r"^house (?=the weather\b)", re.IGNORECASE)


@dataclass(frozen=True)
class WeatherRequest:
    city: str | None = None
    remember: bool = False
    forget: bool = False

    @property
    def is_local(self):
        return self.city is None and not self.remember and not self.forget


def weather_city(text):
    value = re.sub(r"^(?:i(?:['’]m| am) in|in)\s+", "", text.strip(), flags=re.IGNORECASE).strip(" .!?")
    if (2 <= len(value) <= 72 and len(value.split()) <= 7 and value.count(",") <= 2
            and PLACE.fullmatch(value) and not PRIVATE.search(value) and not NOT_PLACE.search(value)):
        return value
    return None


def weather_request(text, history=()):
    value = text.strip()
    value = (spoken_wake_body(value) or value).strip(" .!?")
    lower = value.casefold()
    if lower == "forget my weather city":
        return WeatherRequest(forget=True)
    if (match := REMEMBER.fullmatch(value)) and (city := weather_city(match[1] or match[2])):
        return WeatherRequest(city, remember=True)
    match = QUESTION.fullmatch(value) or TURKISH.fullmatch(value)
    if match is None:
        if history and history[-1].get("engine") == "weather_city_needed" and (city := weather_city(value)):
            return WeatherRequest(city)
        return None
    place = match["city"]
    if place is None:
        return WeatherRequest()
    city = weather_city(place)
    return WeatherRequest(city) if city else None


async def repair_weather_transcript(verifier, audio, language, transcript):
    body = spoken_wake_body(transcript) or transcript
    if MISHEARD_OUTSIDE.fullmatch(body):
        # The fast recognizer can fuse “like outside.” Require the second
        # transcript to support a weather reading before changing the intent.
        checked = await verifier.recover(audio, language, weather=True)
        if (request := weather_request(checked)) is not None and not request.forget and not request.remember:
            return checked
        if OUTSIDE_WORDING.search(checked):
            return "How’s the weather like outside?"
        return ""
    if OUTSIDE_WORDING.search(body) and not weather_request(body):
        checked = await verifier.recover(audio, language)
        if weather_request(checked):
            return checked
    return MISHEARD_WEATHER.sub("How's ", transcript, count=1)
