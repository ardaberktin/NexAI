"""Validate model-selected local dates before reading calendar or archive data."""

from datetime import date, datetime, time, timedelta

from .recall_dates import time_zone

DATE_FIELDS = {"start_date", "end_date"}


def date_selection(tool):
    """Return an inclusive start and exclusive end, capped at one month."""
    start = tool.get("start_date")
    finish = tool.get("end_date")
    if not DATE_FIELDS.intersection(tool):
        return None
    if not isinstance(start, str) or (finish is not None and not isinstance(finish, str)):
        raise ValueError("Provide start_date and optional exclusive end_date as YYYY-MM-DD.")
    try:
        first = date.fromisoformat(start)
        last = date.fromisoformat(finish) if finish is not None else first + timedelta(days=1)
        if first.isoformat() != start or (finish and last.isoformat() != finish) or not 1 <= (last - first).days <= 31:
            raise ValueError
    except (ValueError, OverflowError):
        raise ValueError("Choose ISO dates spanning one to 31 days with an exclusive end_date.") from None
    return first, last


def local_bounds(selection, zone):
    tz = time_zone(zone)
    return tuple(datetime.combine(day, time(), tz) for day in selection)


def clock_bounds(tool, zone):
    if "start_at" not in tool and "end_at" not in tool:
        return None
    try:
        begin = datetime.fromisoformat(tool["start_at"])
        end = datetime.fromisoformat(tool["end_at"])
    except (KeyError, ValueError, TypeError):
        raise ValueError("Supply start_at and end_at as local ISO timestamps.") from None
    if begin.tzinfo or end.tzinfo or not timedelta() < end - begin <= timedelta(days=31):
        raise ValueError("Select a local time range of up to 31 days.")
    tz = time_zone(zone)
    return begin.replace(tzinfo=tz), end.replace(tzinfo=tz)
