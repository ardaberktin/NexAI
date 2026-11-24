"""Have Nex choose a check-in time based on the expected pace of the outcome."""

import re
from dataclasses import replace
from datetime import datetime, time, timedelta

from .goal_schedule import local_start
from .goal_writing import ask, json_object
from .recall_dates import time_zone

CHOOSE_TIMING = """You are Nex. Choose when the user can usefully answer the follow-up question.
Consider how long the outcome normally takes and when the user can reasonably check. Respect dates, windows,
times, and spacing the user requested. Choose a time that gives the outcome time to change and the user
time to check. Consider earlier answers and relevant timing details in the goal and original request.

An explicit reminder time takes priority. Otherwise, start with the expected real-world milestone. If the user
gave an arrival window, allow the end of that window and a chance to check. Without an ETA, allow several
business days for ordinary postal delivery or an application to be processed. For a newly issued document
or card with no dispatch information, allow for both processing and transit: around a working week is a
reasonable first check, adjusted to the facts of the request. Then choose a convenient local
time after the user could reasonably know. A dispatch notice alone does not mean an item has arrived.
A same-day check can make sense for a short task or an item already out for delivery. A deadline may require
checking earlier while the user can still act. After a pending answer, allow time for meaningful progress.
Choose the interval for this particular situation. The date you pick is a check-in, never a delivery estimate.

Return a single JSON object:
- {"days_from_now":D,"clock":"HH:MM"} to pick a practical day and local clock time.
  Replace D with the number of calendar days to add to local_time. Clock is just the 24-hour time,
  without a date. Example format: {"days_from_now":4,"clock":"17:30"}.
- {"start":"YYYY-MM-DDTHH:MM"} for an exact local date and time the user requested.
Choose one form. Use only days_from_now and clock when you are choosing the time.
"""


def choose_time(generate, schedule, data, anchor):
    zone = time_zone(data["time_zone"])
    local = anchor.astimezone(zone)
    timing = {key: data[key] for key in ("goal", "original_request", "local_time", "time_zone",
                                        "first_run", "next_run", "earlier_answers", "latest_answer")}
    timing["question"] = schedule.question
    instructions = CHOOSE_TIMING
    for _ in range(2):
        raw = ask(generate, instructions, timing, 100)
        if not raw:
            return None
        try:
            start = choose_start(json_object(raw), local)
            return replace(schedule, due=local_start(start, zone, anchor))
        except (TypeError, ValueError, OverflowError):
            instructions = CHOOSE_TIMING + "\nChoose a future time and use HH:MM only in clock."
    return None


def choose_start(tool, local):
    if tool.keys() == {"start"}:
        return tool["start"]
    days, clock = tool.get("days_from_now"), tool.get("clock")
    relative_fields = {"days_from_now", "clock"}
    if (tool.keys() - {"start"} != relative_fields or type(days) is not int or days < 0
            or not isinstance(clock, str) or not re.fullmatch(r"[0-9]{2}:[0-9]{2}", clock)):
        raise ValueError("Use a local day and clock time.")
    relative = datetime.combine(local.date() + timedelta(days=days), time.fromisoformat(clock))
    if "start" in tool:
        due = local_start(tool["start"], local.tzinfo, local)
        if due.astimezone(local.tzinfo).replace(tzinfo=None) != relative:
            raise ValueError("The absolute and relative follow-up times must agree.")
        return tool["start"]
    return relative.isoformat(timespec="minutes")
