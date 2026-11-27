"""Save the model's validated goal and read back its scheduling state."""

from datetime import UTC, datetime

from ..goal_follow_ups import assessment, for_goal
from ..goal_schedule import display_time
from ..goals import Goals
from ..recall_dates import time_zone
from ..workspace_schema import WorkspaceConflict, WorkspaceMissing
from .goal_plans import GoalPlanError, resolve_goal


def save_goal(store, turn, tool):
    now = datetime.now(UTC)
    try:
        target, start = resolve_goal(turn, tool, now)
    except GoalPlanError as error:
        raise ValueError("I couldn’t confirm a valid time for this goal. Please try again.") from error
    goals = Goals(store)
    try:
        existing = goals.detail(target) if target else None
        data = goal_data(tool, turn.zone, start, existing)
        if data["frequency_minutes"] and not data["scheduled_at"]:
            raise ValueError("When should this repeating goal start?")
        if data["frequency_minutes"] and tool.get("check_in"):
            raise ValueError("Should this repeat on a fixed schedule or only while the outcome is still pending?")
        goal = goals.save(
            target or turn.id, data, anchor=now, create_once=not target,
            request=turn.text, language=turn.language, check_in=tool.get("check_in"), chat_request_id=turn.id,
        )
    except (WorkspaceMissing, WorkspaceConflict) as error:
        raise ValueError(str(error)) from error
    return goal_receipt(store, goal, updated=bool(target)), goal["id"]


def goal_data(tool, zone, start, existing):
    data = {"prompt": tool.get("query"), "frequency_minutes": tool.get("frequency", 0),
            "zone": zone, "scheduled_at": start}
    if "watch_provider" in tool:
        data["watch_provider"] = tool["watch_provider"]
    if existing is not None:
        data.update(title=existing["title"], revision=existing["revision"], prompt=tool.get("query", existing["prompt"]),
                    frequency_minutes=tool.get("frequency", existing["frequency_minutes"]),
                    scheduled_at=start or existing["scheduled_at"])
        if start is None:
            data["zone"] = existing["zone"]
    return data


def goal_receipt(store, goal, *, updated):
    with store.connect() as db:
        db.execute("BEGIN")
        follow_up = for_goal(db, goal["id"])
        work = assessment(db, goal["id"])
    due = goal["next_run"]
    if follow_up and follow_up["question"] is None and goal["status"] == "active":
        due = follow_up["due"]
    local = datetime.fromisoformat(due).astimezone(time_zone(goal["zone"])) if due else None
    receipt = {
        "title": goal["title"], "prompt": goal["prompt"], "status": goal["status"], "updated": updated,
        "check_in": bool(work and work["required"]),
        "schedule_text": display_time(local) if local else None,
    }
    if local:
        receipt["next_weekday"] = local.strftime("%A")
    if provider := goal.get("watch_provider"):
        receipt["watch_provider"] = provider
    if frequency := goal["frequency_minutes"]:
        receipt["frequency_minutes"] = frequency
    return receipt
