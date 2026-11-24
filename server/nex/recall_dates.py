import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

EXPLICIT_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
PHRASES = {
    "today": ("day", 0), "bugün": ("day", 0), "bugun": ("day", 0),
    "yesterday": ("day", -1), "dün": ("day", -1), "dun": ("day", -1),
    "this week": ("week", 0), "bu hafta": ("week", 0),
    "last week": ("week", -1), "geçen hafta": ("week", -1), "gecen hafta": ("week", -1),
    "this month": ("month", 0), "bu ay": ("month", 0),
    "last month": ("month", -1), "geçen ay": ("month", -1), "gecen ay": ("month", -1),
}
RELATIVE_DATE = re.compile(r"\b(?:" + "|".join(PHRASES) + r")\b", re.IGNORECASE)


def time_key(value):
    return value.astimezone(UTC).isoformat(timespec="milliseconds")


def time_zone(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_+/-]{1,96}", value):
        raise ValueError("Use a valid time zone name.")
    try:
        return ZoneInfo(value)
    except (ValueError, ZoneInfoNotFoundError):
        raise ValueError("Use a valid time zone name.") from None


def month_offset(day, offset):
    year, month = divmod(day.year * 12 + day.month - 1 + offset, 12)
    return date(year, month + 1, 1)


def relative_period(today, unit, shift):
    if unit == "week":
        start = today - timedelta(days=today.weekday()) + timedelta(weeks=shift)
        return start, start + timedelta(weeks=1)
    if unit == "month":
        return month_offset(today, shift), month_offset(today, shift + 1)
    start = today + timedelta(days=shift)
    return start, start + timedelta(days=1)


@dataclass(frozen=True)
class RecallDates:
    query: str
    bounds: tuple[str, str]
    local_dates: tuple[str, str]
    zone: str


def recall_dates(query, zone="UTC", asked_at=None):
    match = EXPLICIT_DATE.search(query)
    if match:
        try:
            start = date.fromisoformat(match[0])
            finish = start + timedelta(days=1)
        except (ValueError, OverflowError):
            return None
    else:
        match = RELATIVE_DATE.search(query)
        if not match:
            return None
        reference = (asked_at or datetime.now(UTC)).astimezone(time_zone(zone)).date()
        start, finish = relative_period(reference, *PHRASES[match[0].casefold()])
    tz = time_zone(zone)
    try:
        bounds = tuple(datetime.combine(day, time(), tz).astimezone(UTC).isoformat(timespec="milliseconds")
                       for day in (start, finish))
    except OverflowError:
        return None
    topic = (query[:match.start()] + " " + query[match.end():]).strip(" ?.,;:-")
    return RecallDates(topic, bounds, (start.isoformat(), finish.isoformat()), zone)
