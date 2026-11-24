"""Resolve a private goal action and commit only the verified change."""

import asyncio

from ..conversation_history import prompt_history
from ..goal_writing import json_object, model_messages, yes_no
from ..goals import Goals
from ..workspace_features import FeatureDisabled
from ..workspace_schema import WorkspaceConflict, WorkspaceMissing
from .goal_context import clarification_exchange
from .goal_edit import plan_edit
from .goal_lookup import PAGE_SIZE, describe, named_targets, saved_goals, shortlist
from .goal_management_prompts import (
    MANAGE_GOALS,
    SELECT_GOAL,
    VERIFY_CHANGE,
    VERIFY_NAMED_DELETE,
)
from .goal_tools import goal_receipt
from .tools import Evidence


def outcome(status, **details):
    return Evidence({"status": status, "changed": False, **details}, engine="goal_management")


def apply_change(store, turn, goal, action, edit):
    goals = Goals(store)
    identifier, revision = goal["id"], goal["revision"]
    if action == "delete":
        goals.delete(identifier, revision)
        return Evidence({"action": "deleted", "title": goal["title"], "schedule_text": None},
                        engine="goal_deleted")
    if action == "keep":
        saved = goals.detail(identifier)
        receipt = {"action": "unchanged", "title": saved["title"], "status": saved["status"], "schedule_text": None}
    elif action == "complete":
        saved = goals.action(identifier, "completed", revision)
        receipt = {"action": "completed", "title": saved["title"], "schedule_text": None}
    else:
        data, required = edit
        request = turn.text if data["prompt"] != goal["prompt"] else goal["request"] or goal["prompt"]
        saved = goals.save(identifier, data, request=request,
                           language=turn.language, check_in=required)
        receipt = goal_receipt(store, saved, updated=True) | {"action": "updated"}
    return Evidence(receipt, engine="goal_" + receipt["action"], goal_id=identifier)


def parse_decision(result, candidates):
    action = result.get("action")
    if result == {"action": "none"}:
        return outcome("no_change_requested")
    if action == "clarify" and result.keys() == {"action", "question"}:
        question = result["question"]
        if isinstance(question, str) and 0 < len(question.strip()) <= 480 and question.strip().endswith("?"):
            return outcome("clarification_needed", suggested_question=question.strip())
    target = result.get("target")
    title = result.get("title")
    candidate = next((row for row in candidates if row["target"] == target), None)
    if (type(target) is not int or not candidate or not isinstance(title, str) or
            candidate["title"].casefold().rstrip(" .!?") != title.casefold().rstrip(" .!?")):
        raise ValueError("The target number and copied title must identify the same listed goal.")
    keys = {"action", "target", "title"}
    if action not in ("complete", "delete", "edit", "keep") or result.keys() != keys:
        raise ValueError("Choose one supported action and only its arguments.")
    return target


async def manage_goal(store, turn, generate, operation=None):
    try:
        goals = await asyncio.to_thread(saved_goals, store)
        if not goals:
            return outcome("no_saved_goals")
        prompt = turn.prompt()
        context = {key: prompt[key] for key in ("question", "local_time", "language", "history")}
        context["history"] = prompt_history(turn.history, include_tools=False)[-4:]
        context["goal_time_zone"] = turn.zone
        if exchange := clarification_exchange(turn.history):
            context["clarification"] = prompt_history(exchange, include_tools=False)
        if operation:
            context["requested_action"] = operation
        recent = next((item["goal_id"] for item in reversed(turn.history) if item.get("goal_id")), None)
        descriptions = [describe(goal, i, recent) for i, goal in enumerate(goals, 1)]
        named = named_targets(turn.text, descriptions)
        if operation in {"delete", "complete", "keep"} and named:
            descriptions = [row for row in descriptions if row["target"] in named]
        check_matches = operation == "delete" and len(named) != 1
        matches = await shortlist(descriptions, context, generate, verify_match=check_matches)
        if not matches:
            return outcome("no_matching_goal")
        if len(matches) > PAGE_SIZE:
            return outcome("ambiguous_goal")
        if check_matches and len(matches) > 1:
            return outcome("ambiguous_goal", candidate_titles=[row["title"] for row in matches])
        return await decide_change(store, turn, generate, goals, context | {"goals": matches}, named)
    except (FeatureDisabled, ValueError, WorkspaceConflict, WorkspaceMissing) as error:
        return outcome("change_rejected", detail=str(error))


async def decide_change(store, turn, generate, goals, context, named):
    operation = context.get("requested_action")
    guidance = SELECT_GOAL if operation else MANAGE_GOALS
    instructions = guidance
    selection = dict(context)
    if operation:
        selection.pop("history")
        selection["goals"] = [{k: v for k, v in row.items() if k != "last_discussed_goal"} for row in context["goals"]]
    for _ in range(3):
        result = json_object(await generate(model_messages(instructions, selection), 280) or "")
        if (result.get("action") in ("use_history", "clarify") and "history" not in selection
                and context["history"] and not named):
            selection = context
            continue
        if result == {"action": "use_history"}:
            return outcome("ambiguous_goal")
        if operation and result.keys() == {"target", "title"}:
            result["action"] = operation
        try:
            target = parse_decision(result, context["goals"])
            if isinstance(target, Evidence):
                return target
            goal = goals[target - 1]
            action = result["action"]
            if operation and operation != action:
                raise ValueError("Return only the selected target and its copied title, none, or clarify.")
            if action in {"delete", "complete", "keep"} and named and target not in named:
                raise ValueError("Use the goal named in the latest message or clarify.")
            edit = None
            if action == "edit":
                edit = await plan_edit(turn, goal, context, generate)
                if isinstance(edit, str):
                    return outcome("clarification_needed", suggested_question=edit)
                if edit is None:
                    return outcome("unverified_edit")
                changes, edit = edit
                result["changes"] = changes
        except ValueError as error:
            instructions = f"{guidance}\nCorrect the previous proposal {result!r}. {error}"
            continue
        verdict = await confirmed_change(goal, result, selection, generate, named)
        if verdict is True:
            return await asyncio.to_thread(apply_change, store, turn, goal, action, edit)
        if verdict is None and "history" not in selection and context["history"] and not named:
            selection = context
            continue
        instructions = f"{guidance}\nThe verifier rejected {result!r}. Correct the action or target, or clarify."
    return outcome("unverified_change")


async def confirmed_change(goal, decision, context, generate, named):
    action = decision["action"]
    if action == "delete" and (named == [decision["target"]] or context.get("requested_action") == "delete"):
        data = {"goal": goal["title"], "user_message": context["question"]}
        if clarification := context.get("clarification"):
            data["preceding_clarification"] = clarification
        return yes_no(await generate(model_messages(VERIFY_NAMED_DELETE, data), 8)) is True
    operation = {"edit": decision["changes"]} if action == "edit" else action
    review = {"saved_goal": {"name": goal["title"], "task": goal["prompt"]}, "proposed_operation": operation,
              "local_time": context["local_time"], "latest_message": context["question"]}
    if action == "edit" and (goal["check_in_due"] or goal["scheduled_at"]):
        review["previous_schedule"] = describe(goal, decision["target"], None).get("scheduled_local")
    for _ in range(2):
        raw = await generate(model_messages(VERIFY_CHANGE, review), 12)
        verdict = raw.strip().rstrip(".").upper() if isinstance(raw, str) else ""
        if verdict == "NEED_CONTEXT" and "history" not in context:
            return None
        if verdict != "NEED_CONTEXT" or not context.get("history") or "history" in review:
            return verdict == "MATCH"
        review["history"] = context["history"]
    return False
