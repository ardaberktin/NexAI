"""Resolve the requested PDF setting and its saved goal together in private Chat."""

import asyncio

from ..goal_writing import json_object, model_messages
from ..goals import Goals
from .goal_lookup import PAGE_SIZE, clarify_target, outcome
from .goal_management_prompts import RESOLVE_PDF_REMOVAL
from .tools import Evidence


def numbered_title(row):
    return f'{row["target"]}. {row["title"]}'


def removal_schema(goals):
    checklist = {"type": "object", "required": list(goals), "additionalProperties": False,
                 "properties": {label: {"type": "boolean"} for label in goals}}
    return {"anyOf": [
        {"type": "object", "required": ["action", "reason"], "additionalProperties": False,
         "properties": {"action": {"const": "keep_settings"}, "reason": {"type": "string"}}},
        {"type": "object", "required": ["action", "subject", "goals"], "additionalProperties": False,
         "properties": {"action": {"const": "disable_future_pdfs"}, "subject": {"type": "string"}, "goals": checklist}},
    ]}


def removal_matches(decision, page):
    """Accept only an explicit decision for every number/title pair on this page."""
    if (decision.keys() == {"action", "reason"} and decision["action"] == "keep_settings"
            and isinstance(decision["reason"], str) and decision["reason"].strip()):
        return outcome("no_change_requested")
    if (decision.keys() != {"action", "subject", "goals"} or decision["action"] != "disable_future_pdfs"
            or not isinstance(decision["subject"], str)):
        raise ValueError("Supply keep_settings with a reason or disable_future_pdfs with subject and goals.")
    selections = decision["goals"]
    if not isinstance(selections, dict) or selections.keys() != {numbered_title(row) for row in page}:
        raise ValueError("Assess every listed goal using its numbered title.")
    if any(type(selected) is not bool for selected in selections.values()):
        raise ValueError("Use true or false for each goal.")
    return [row for row in page if selections[numbered_title(row)]]


async def decide_page(page, context, generate):
    goals = {numbered_title(row): row["task"] for row in page}
    instructions = RESOLVE_PDF_REMOVAL
    exchange = context.get("clarification")
    if exchange:
        data = {"request": exchange[0]["content"], "goals": goals}
    else:
        data = {"goals": goals, "latest_message": context["question"]}
        if history := context.get("history"):
            data["history"] = history
    data["current_settings"] = {numbered_title(row): {"pdf_enabled": bool(row.get("pdf_instruction")),
                                                       "last_discussed": bool(row.get("last_discussed_goal"))}
                                for row in page}
    schema = removal_schema(goals)
    tokens = 180 + sum(len(label.encode()) for label in goals)
    for _ in range(2):
        messages = model_messages(instructions, data, schema=schema)
        if exchange:
            messages.extend(exchange[1:])
            messages.append({"role": "user", "content": context["question"]})
        raw = await generate(messages, tokens)
        if not raw:
            break
        try:
            return removal_matches(json_object(raw), page)
        except ValueError as error:
            instructions += f"\nCorrect the output format: {error}"
    return outcome("model_unavailable")


async def remove_pdf(store, goals, candidates, context, generate):
    matches = []
    for offset in range(0, len(candidates), PAGE_SIZE):
        result = await decide_page(candidates[offset:offset + PAGE_SIZE], context, generate)
        if isinstance(result, Evidence):
            return result
        matches.extend(result)
        if len(matches) > PAGE_SIZE:
            return outcome("clarification_needed")
    if not matches:
        return outcome("no_matching_goal")
    if len(matches) > 1:
        return clarify_target(matches, goals)
    goal = goals[matches[0]["target"] - 1]
    saved = await asyncio.to_thread(Goals(store).set_pdf_format, goal["id"], goal["revision"], None)
    receipt = {"action": "pdf_removed", "title": saved["title"], "changed": bool(goal.get("pdf_instruction")),
               "schedule_text": None}
    return Evidence(receipt, engine="goal_pdf_removed", goal_id=saved["id"])
