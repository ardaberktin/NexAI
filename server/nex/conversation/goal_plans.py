"""Validate the local model's goal proposals against the current turn."""

import re
from datetime import UTC, datetime

from ..goal_schedule import local_start
from ..goal_writing import model_messages, yes_no
from ..recall_dates import time_zone
from ..reminder_schedule import SCHEDULE, resolve_time
from .catalog import CHOOSE_GOAL_TARGET, VERIFY_GOAL_PLAN
from .goal_context import goal_references

LOCAL_START = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}(?:Z|[+-][0-9]{2}:[0-9]{2})?")


class GoalPlanError(ValueError):
    pass


def validate_goal_action(tools, review):
    if not review or not tools:
        return
    tool, *other = tools
    if review.get("edit_target") and tool["name"] not in {"goal", "manage_goal"}:
        raise GoalPlanError("Return a goal update for the matched edit_target.")
    if review.get("mail_watch") and (
        other or tool["name"] != "goal" or not tool.get("frequency") or "target" in tool
    ):
        raise GoalPlanError("For an email watch, return one new repeating goal with a future start.")


def target_id(turn, tool):
    if "target" not in tool:
        return None
    references = goal_references(turn.history)
    index = tool["target"] - 1
    if not 0 <= index < len(references):
        raise GoalPlanError("Use a target from goal_references or ask which goal to update.")
    return references[index][0]


def goal_start(turn, tool, anchor):
    timing = " ".join(tool.get("timing", "").casefold().split())
    start = tool.get("start")
    request = " ".join(turn.text.casefold().split())
    if "timing" in tool and (not start or not timing or
                             not re.search(r"(?<!\w)" + re.escape(timing) + r"(?!\w)", request)):
        error = "Timing must copy the words in the latest request that ask for this start."
        phrases = list(dict.fromkeys(match[0] for match in SCHEDULE.finditer(turn.text)))[:4]
        if phrases:
            error += f" Possible timing phrases copied verbatim: {phrases!r}. Use one only if it requests the start."
        raise GoalPlanError(error)
    if start is None:
        return None
    if not tool.get("frequency") and not timing:
        raise GoalPlanError("For a one-time start, quote the requested time in timing, or omit start and timing.")
    try:
        if not LOCAL_START.fullmatch(start):
            raise ValueError
        return local_start(start, time_zone(turn.zone), anchor).isoformat()
    except ValueError:
        raise GoalPlanError(
            "Use a valid future local time in YYYY-MM-DDTHH:MM. "
            "Ask for clarification if the requested time has passed. Omit start only if timing was unspecified."
        ) from None


def resolve_goal(turn, tool, anchor):
    target = target_id(turn, tool)
    if tool.get("check_in") and tool.get("frequency"):
        raise GoalPlanError("Use frequency:0 for an outcome check-in. Put the requested spacing in query.")
    if tool.get("frequency") and "target" not in tool and not tool.get("start"):
        raise GoalPlanError("For a repeating goal, choose a future first run in start.")
    return target, goal_start(turn, tool, anchor)


async def review_goal_plan(turn, tool, generate, data):
    target, start = resolve_goal(turn, tool, max(turn.started, datetime.now(UTC)))
    if tool.get("watch_provider") and not data.get("mail_watch"):
        raise GoalPlanError("Leave the watch provider to the email-watch planner.")
    if data.get("edit_target") is not None and tool.get("target") != data["edit_target"]:
        raise GoalPlanError("Use the matched edit_target in the goal tool.")
    if not (target or data["goal_references"]) and known_new_goal_schedule(turn, tool, start, data):
        return
    review = {**data, "proposal": tool}
    if yes_no(await generate(model_messages(VERIFY_GOAL_PLAN, review), 8)) is not True:
        raise GoalPlanError(
            "Recheck the requested timing, whether the user needs a check-in, and any referenced goal. "
            "Ask a clarification if needed."
        )


def known_new_goal_schedule(turn, tool, start, data):
    if tool.get("check_in"):
        return start is None
    if tool.get("frequency") and "timing" not in tool:
        return True
    match = SCHEDULE.fullmatch(tool["timing"].strip().rstrip(".,?!")) if tool.get("timing") else None
    if not match:
        return False
    local = datetime.fromisoformat(data["local_time"]).astimezone(time_zone(turn.zone))
    try:
        expected = resolve_time(match, local).astimezone(UTC).replace(second=0, microsecond=0)
    except (ValueError, OverflowError):
        return False
    if expected != datetime.fromisoformat(start):
        raise GoalPlanError("The proposed start differs from the time quoted in timing.")
    return True


async def choose_goal_target(context, generate):
    references = context["goal_references"]
    fields = ("target", "title", "status", "next_check_in")
    candidates = [{key: ref[key] for key in fields if key in ref} for ref in references]
    data = {"question": context["question"], "goal_references": candidates}
    raw = await generate(model_messages(CHOOSE_GOAL_TARGET, data), 8)
    value = raw.strip() if isinstance(raw, str) else None
    return next((ref["target"] for ref in references if str(ref["target"]) == value), None)
