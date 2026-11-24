"""Calculate age in calendar units and elapsed time at the device's local date."""

import calendar
from datetime import UTC, datetime

from .birthdate import BirthDate, birth_instant, known_birth_date
from .recall_dates import month_offset, time_zone

SECONDS = {"hour": 3600, "minute": 60, "second": 1}


def calendar_age(born, today):
    """Anchor each month to the birth day, clamping to the last day of a shorter month."""
    def anniversary_in(month):
        return month.replace(day=min(born.day, calendar.monthrange(month.year, month.month)[1]))

    anniversary = anniversary_in(today)
    if anniversary > today:
        anniversary = anniversary_in(month_offset(today, -1))
    months = (anniversary.year - born.year) * 12 + anniversary.month - born.month
    return months, (today - anniversary).days


def quantity(count, unit):
    return f"{count:,} {unit}{'' if count == 1 else 's'}"


def joined(parts, conjunction):
    if len(parts) < 2:
        return parts[0]
    return ", ".join(parts[:-1]) + (" " if len(parts) == 2 else ", ") + conjunction + " " + parts[-1]


def calendar_parts(months, remainder, units):
    parts = []
    if "year" in units:
        years, months = divmod(months, 12)
        parts.append(quantity(years, "year"))
    if "month" in units or parts:
        parts.extend((quantity(months, "month"), quantity(remainder, "day")))
    return parts


def elapsed_totals(birth, reference, units):
    days = (reference.date() - birth.day).days
    totals = []
    seconds = days * 86400
    if birth.instant:
        elapsed = reference.astimezone(UTC) - birth.instant
        days, seconds = elapsed.days, int(elapsed.total_seconds())
    for unit in units:
        if unit == "week":
            totals.append(quantity(days // 7, "full week"))
        elif unit == "day":
            totals.append(quantity(days, "day"))
        elif unit in SECONDS:
            prefix = "" if birth.instant else "about "
            totals.append(prefix + quantity(seconds // SECONDS[unit], unit))
    return totals


def age_text(birth, reference, units):
    today = reference.date()
    months, remainder = calendar_age(birth.day, today)
    as_of = f"As of {today:%B} {today.day}, {today.year}, "
    if units == ("year",):
        return as_of + f"you’re {quantity(months // 12, 'year')} old."
    if today == birth.day and not birth.instant and any(unit in SECONDS for unit in units):
        return as_of + "you were born today. A more precise age needs your birth time and time zone."
    totals = elapsed_totals(birth, reference, units)
    if parts := calendar_parts(months, remainder, units):
        text = as_of + "you’re " + joined(parts, "and") + " old."
        if totals:
            text += " That’s " + joined(totals, "or") + " in total."
    else:
        text = as_of + "you’ve lived " + joined(totals, "or") + "."
    if not birth.instant and any(unit in SECONDS for unit in units):
        text += " I used 24 hours per calendar day. An exact count needs your birth time and time zone."
    return text


def age_reply(files, request, history, asked_at, zone):
    birth = request.birth or known_birth_date(files, history)
    if birth and request.clock:
        birth = BirthDate(birth.day, birth_instant(birth.day, request.clock))
    reference = (asked_at or datetime.now(UTC)).astimezone(time_zone(zone))
    if not birth:
        text = "What’s your date of birth, including the year?"
    elif birth.day > reference.date() or (birth.instant and birth.instant > reference):
        text = "That birth date is in the future. What date of birth should I use?"
    else:
        text = age_text(birth, reference, request.units)
    return {"text": text, "language": "en", "sources": [], "engine": "calculation"}
