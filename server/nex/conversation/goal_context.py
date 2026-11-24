"""Give Nex current, persisted facts for goals mentioned in private Chat."""

from datetime import datetime

from ..conversation_history import MAX_EXCHANGES
from ..goal_writing import json_value
from ..goals import Goals
from ..recall_dates import time_zone
from ..store import excerpt
from ..workspace_schema import WorkspaceMissing


def clarification_exchange(history):
    if len(history) < 2:
        return ()
    question, answer = history[-2:]
    if (question["kind"] == "question" and answer.get("private") and not answer.get("goal_id")
            and "manage_goal" in answer.get("selected_tools", ()) and "?" in answer["text"]):
        return question, answer
    return ()


def pending_pdf_edit(history):
    if not (exchange := clarification_exchange(history)):
        return False
    tools = json_value(exchange[1].get("selected") or "")
    return isinstance(tools, list) and any(
        isinstance(tool, dict) and tool.get("name") == "manage_goal" and tool.get("format_only") is True
        for tool in tools
    )


def with_goal_state(store, history):
    goals = Goals(store)
    states = {}
    enriched = []
    for item in history:
        identifier = item.get("goal_id") if item.get("private") else None
        if identifier and identifier not in states:
            states[identifier] = load_goal_state(goals, identifier)
        if identifier:
            state = states[identifier]
            item = dict(item, goal_state=state) if state else dict(item, goal_unavailable=True)
        enriched.append(item)
    return tuple(enriched)


def load_goal_state(goals, identifier):
    try:
        goal = goals.detail(identifier)
    except WorkspaceMissing:
        return None
    zone = time_zone(goal["zone"])
    state = {"title": goal["title"], "prompt": excerpt(goal["prompt"], 320), "status": goal["status"],
             "frequency_minutes": goal["frequency_minutes"], "time_zone": zone.key}
    if next_run := goal["next_run"]:
        state["next_run"] = datetime.fromisoformat(next_run).astimezone(zone).isoformat(timespec="minutes")
    if follow_up := goal["follow_up"]:
        if follow_up["question"] is None:
            local = datetime.fromisoformat(follow_up["due"]).astimezone(zone)
            state["next_check_in"] = local.isoformat(timespec="minutes")
        elif follow_up["can_answer"]:
            state["awaiting_check_in_answer"] = True
    return state


def goal_references(history):
    recent = {}
    history = history[-2 * MAX_EXCHANGES:]
    for index, item in reversed(tuple(enumerate(history))):
        identifier = item.get("goal_id")
        if identifier and item["kind"] == "answer" and item.get("private") and not item.get("goal_unavailable"):
            question = history[index - 1] if index else {}
            recent.setdefault(identifier, {
                "request": excerpt(question["text"], 240) if question.get("kind") == "question" else "",
                **(item.get("goal_state") or {}),
            })
    return tuple(recent.items())
