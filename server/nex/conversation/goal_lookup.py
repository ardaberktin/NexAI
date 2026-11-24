"""Read and shortlist saved goals inside an authorized goal-management turn."""

import re
from datetime import datetime

from ..goal_definition import summary
from ..goal_writing import json_object, model_messages
from ..recall_dates import time_zone
from ..store import excerpt, normalized
from ..workspace_features import require
from .goal_management_prompts import FIND_GOALS

PAGE_SIZE = 8


def saved_goals(store):
    with store.connect() as db:
        db.execute("BEGIN")
        require(db, "goals")
        rows = db.execute("""
            SELECT g.*, w.request, w.required, f.due AS check_in_due FROM goals g
            LEFT JOIN goal_follow_up_work w ON w.goal_id=g.id
            LEFT JOIN goal_follow_ups f ON f.goal_id=g.id AND f.cancelled=0 AND f.selected_choice IS NULL
            ORDER BY g.updated DESC, g.id
        """)
        return [summary(row) | {"request": row["request"], "check_in": bool(row["required"]),
                                "check_in_due": row["check_in_due"]} for row in rows]


def describe(goal, target, recent):
    data = {"target": target, "title": goal["title"], "task": excerpt(goal["prompt"], 300),
            "status": goal["status"], "frequency": goal["frequency_minutes"], "zone": goal["zone"]}
    if goal["id"] == recent:
        data["last_discussed_goal"] = True
    if goal["request"]:
        data["request"] = excerpt(goal["request"], 200)
    if due := goal["check_in_due"] or goal["next_run"] or goal["scheduled_at"]:
        local = datetime.fromisoformat(due).astimezone(time_zone(goal["zone"]))
        data["scheduled_local"] = local.isoformat(timespec="minutes")
    return data


async def shortlist(descriptions, context, generate, *, verify_match=False):
    if len(descriptions) <= PAGE_SIZE and not verify_match:
        return descriptions
    matches = []
    for offset in range(0, len(descriptions), PAGE_SIZE):
        page = descriptions[offset:offset + PAGE_SIZE]
        labels = [row["title"] if row["title"].casefold() == row["task"].casefold()
                  else f"{row['title']} — {row['task']}" for row in page]
        data = {"goals": labels, "message": context["question"]}
        if context.get("history"):
            data["history"] = context["history"]
        messages = model_messages(FIND_GOALS, data)
        result = json_object(await generate(messages, 100) or "")
        mask = result.get("matches")
        if not isinstance(mask, list) or len(mask) != len(page) or any(type(item) is not bool for item in mask):
            raise ValueError("I couldn’t identify the matching goals. Please try again.")
        matches.extend(row for row, matched in zip(page, mask, strict=True) if matched)
        if len(matches) > PAGE_SIZE:
            break
    return matches


def named_targets(question, goals):
    """Ground literal saved names on whole words, independently of the model's candidate numbering."""
    words = " " + " ".join(re.findall(r"\w+", normalized(question))) + " "
    found = []
    for goal in goals:
        title = " ".join(re.findall(r"\w+", normalized(goal["title"])))
        if title and f" {title} " in words:
            found.append(goal["target"])
    return found
