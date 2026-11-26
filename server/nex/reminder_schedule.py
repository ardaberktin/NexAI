"""Resolve common reminder times against the user's local clock."""

import re
from datetime import UTC, date, datetime, time, timedelta

COUNTS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
          "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "a couple of": 2}
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
DELAY = re.compile(
    rf"(?:in|after) (?:about |around |roughly )?(?P<count>[0-9]{{1,4}}|{'|'.join(COUNTS)}) "
    r"(?P<unit>minutes?|hours?|days?|weeks?)(?: or so)?(?: from now)?",
    re.IGNORECASE,
)
MERIDIEM = r"[ap]\.?m\.?"
CLOCK = rf"(?:noon|midnight|(?:[01]?\d|2[0-3]):[0-5]\d(?:\s?{MERIDIEM})?|(?:0?[1-9]|1[0-2])\s?{MERIDIEM})"
DAY = (rf"(?:(?:on |this |next )?(?:{'|'.join(WEEKDAYS)})|(?:on )?\d{{4}}-\d{{2}}-\d{{2}}"
       r"|today|tonight|tomorrow|next week)")
SCHEDULE = re.compile(
    rf"\b(?:(?P<delay>{DELAY.pattern})(?: at (?P<delay_clock>{CLOCK}))?"
    rf"|(?P<day>{DAY})(?: at (?P<day_clock>{CLOCK}))?|at (?P<clock>{CLOCK}))\b", re.IGNORECASE,
)
FOLLOWED_BY_SUBJECT = re.compile(r"^(?:to|about|if|whether)\b", re.IGNORECASE)


class ReminderTimeError(ValueError):
    pass


def clock_time(value):
    value = value.replace(" ", "").replace(".", "").lower()
    if value in ("noon", "midnight"):
        return time(12 if value == "noon" else 0)
    meridiem = value[-2:] if value.endswith(("am", "pm")) else None
    parts = (value[:-2] if meridiem else value).split(":")
    hour = int(parts[0])
    if meridiem:
        if not 1 <= hour <= 12:
            raise ValueError("Invalid reminder hour")
        hour = hour % 12 + (12 if meridiem == "pm" else 0)
    return time(hour, int(parts[1]) if len(parts) == 2 else 0)


def reminder_day(value, today):
    day = value.lower().split()[-1]
    if day in WEEKDAYS:
        days = (WEEKDAYS.index(day) - today.weekday()) % 7
        return today + timedelta(days=days or 7)
    if day in ("today", "tonight", "tomorrow", "week"):
        return today + timedelta(days={"tomorrow": 1, "week": 7}.get(day, 0))
    return date.fromisoformat(day)


def resolve_time(match, local):
    if match["delay"]:
        amount = match["count"].lower()
        count = int(amount) if amount.isdecimal() else COUNTS[amount]
        unit = match["unit"].lower().rstrip("s") + "s"
        period = timedelta(**{unit: count})
        if match["delay_clock"]:
            return datetime.combine((local + period).date(), clock_time(match["delay_clock"]), local.tzinfo)
        if unit in ("minutes", "hours"):
            return (local.astimezone(UTC) + period).astimezone(local.tzinfo)
        return local + period
    if match["day"]:
        default_hour = 19 if match["day"].lower() == "tonight" else 9
        chosen = clock_time(match["day_clock"]) if match["day_clock"] else time(default_hour)
        return datetime.combine(reminder_day(match["day"], local.date()), chosen, local.tzinfo)
    chosen = datetime.combine(local.date(), clock_time(match["clock"]), local.tzinfo)
    return chosen if chosen > local else chosen + timedelta(days=1)


def reminder_timing(subject, local):
    """Take timing from either end, or before a clause giving the reminder's subject."""
    for match in SCHEDULE.finditer(subject):
        before = subject[:match.start()].strip(" ,.;")
        after = subject[match.end():].strip(" ,.;")
        if after.startswith(("'", "’")) or (before and after and not FOLLOWED_BY_SUBJECT.match(after)):
            continue
        try:
            return " ".join(filter(None, (before, after))), resolve_time(match, local)
        except (ValueError, OverflowError):
            raise ReminderTimeError from None
    return subject, None


def default_check_in(local):
    return datetime.combine(local.date() + timedelta(days=7), time(9), local.tzinfo)
