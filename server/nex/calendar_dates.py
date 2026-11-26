"""Recognize calendar dates in source text and personal date statements."""

import calendar
import re
from datetime import date

MONTHS = {name.casefold(): i for names in (calendar.month_name, calendar.month_abbr)
          for i, name in enumerate(names) if name}
MONTHS.update({name: i for i, name in enumerate(
    ["ocak", "şubat", "mart", "nisan", "mayıs", "haziran", "temmuz", "ağustos", "eylül", "ekim", "kasım", "aralık"], 1)})
MONTH = "(?:" + "|".join(MONTHS) + r")\.?"
ORDINAL = r"(?:st|nd|rd|th)?"
DATE = re.compile(rf"\b(?:(\d{{4}})-(\d{{2}})-(\d{{2}})|({MONTH}) (\d{{1,2}}){ORDINAL}|"
                  rf"(\d{{1,2}}){ORDINAL} ({MONTH}))(?:,? (\d{{4}}))?\b", re.IGNORECASE)


def date_parts(match):
    year, month, day, first_month, first_day, other_day, other_month, last_year = match.groups()
    month_number = int(month) if month else MONTHS[(first_month or other_month).lower().rstrip(".")]
    return int(year or last_year) if year or last_year else None, month_number, int(day or first_day or other_day)


def date_mentions(text):
    return (date_parts(match) for match in DATE.finditer(text))


def full_date(match):
    year, month, day = date_parts(match)
    if year is not None:
        try:
            return date(year, month, day)
        except ValueError:
            pass
    return None


def calendar_dates(text):
    return {day for match in DATE.finditer(text) if (day := full_date(match))}
