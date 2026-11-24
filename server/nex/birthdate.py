"""Read an owner's stated birth date from a turn or a personal Memory entry."""

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone

from .calendar_dates import DATE, full_date
from .recall_dates import time_zone
from .wake import LEADING_WAKE

LABEL = r"(?:birthday|date of birth|birth\s?date|dob)"
OWNER = re.compile(rf"^(?:i was born\b(?: on)?|my {LABEL}\b(?: (?:is|was))?)\s*[:—-]?\s*", re.IGNORECASE)
FIELD_VALUE = rf"(?:(?:(?:the user|user|i) )?(?:was )?born(?: on)?|(?:my|the user['’]s) {LABEL} (?:is|was))"
MEMORY_FIELD = re.compile(rf"^{LABEL}\s*[:—-]\s*(?:{FIELD_VALUE}\s+)?", re.IGNORECASE)
REMEMBER = re.compile(r"^(?:please )?remember(?: that\b|\s*:)\s*", re.IGNORECASE)
CLOCK = re.compile(r"(?:(?:i was born )?at )?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s+(?:in )?"
                   r"(UTC|GMT|(?:UTC)?[+-]\d\d:\d\d|[A-Za-z_]+(?:/[A-Za-z_+-]+)+)", re.IGNORECASE)


@dataclass(frozen=True)
class BirthDate:
    day: date
    instant: datetime | None = None


def birth_instant(day, clock):
    hour, minute, period, zone = clock.groups()
    hour, minute = int(hour), int(minute or 0)
    if period:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + 12 * (period.lower() == "pm")
    try:
        if offset := re.fullmatch(r"(?:UTC)?([+-])(\d\d):(\d\d)", zone, re.IGNORECASE):
            if int(offset[3]) > 59:
                return None
            delta = timedelta(hours=int(offset[2]), minutes=int(offset[3]))
            tz = timezone(-delta if offset[1] == "-" else delta)
        else:
            tz = time_zone(zone.upper() if zone.upper() in {"UTC", "GMT"} else zone)
        local = datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)
        if local.utcoffset() == local.replace(fold=1).utcoffset():
            return local.astimezone(UTC)
    except (OverflowError, ValueError):
        pass
    # A skipped or repeated local time cannot establish one exact instant.
    return None


def birth_date(text, *, bare=False, memory=False):
    value = LEADING_WAKE.sub("", text.strip(), count=1)
    if memory:
        value = value.removeprefix("- ").replace("**", "").strip()
    value = REMEMBER.sub("", value, count=1)
    label = OWNER.match(value) or (memory and MEMORY_FIELD.match(value))
    if label:
        value = value[label.end():]
    elif not bare:
        return None
    match = DATE.match(value)
    if not match or not (day := full_date(match)):
        return None
    suffix = value[match.end():].strip(" .,?!")
    clock = CLOCK.fullmatch(suffix)
    if bare and not label and suffix and not clock:
        return None
    return BirthDate(day, birth_instant(day, clock) if clock else None)


def known_birth_date(files, history):
    for i in range(len(history) - 1, -1, -1):
        turn = history[i]
        if turn["kind"] == "question":
            # A bare date is evidence only if it previously completed an age calculation.
            bare = i + 1 < len(history) and history[i + 1].get("engine") == "calculation"
            if stated := birth_date(turn["text"], bare=bare):
                return stated
    with files.locked():
        lines = files.read("memory")["markdown"].splitlines()
    entries = {entry for line in lines if (entry := birth_date(line, memory=True))}
    if len({entry.day for entry in entries}) == 1:
        instants = {entry.instant for entry in entries if entry.instant}
        return BirthDate(next(iter(entries)).day, instants.pop() if len(instants) == 1 else None)
    return None
