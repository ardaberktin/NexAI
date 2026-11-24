"""Plan and validate a partial edit to a selected saved goal."""

from dataclasses import replace
from datetime import UTC, datetime

from ..goal_writing import json_object, model_messages
from .calendar_scope import scope_prompt, weekday_selector, with_weekday
from .goal_context import clarification_exchange
from .goal_lookup import describe
from .goal_management_prompts import EDIT_GOAL
from .goal_plans import goal_start
from .goal_tools import goal_data
from .plan import validated


def changes_for(turn, goal, changes):
    if not isinstance(changes, dict) or not changes or changes.keys() - {
        "query", "title", "start", "timing", "frequency", "check_in", "format_instruction"
    }:
        raise ValueError("Specify the goal fields to change.")
    fields = dict(changes)
    title = fields.pop("title", None)
    if "title" in changes and (not isinstance(title, str) or not 1 <= len(title.strip()) <= 120):
        raise ValueError("Use a nonempty title of up to 120 characters.")
    if "format_instruction" in fields:
        instruction = fields.pop("format_instruction")
        if not isinstance(instruction, str) or not instruction.strip() or "query" in fields:
            raise ValueError("Use format_instruction on its own to change the output format while keeping the task.")
        task = goal["prompt"]
        instruction = instruction.strip()
        fields["query"] = task if task.endswith(instruction) else f"{task}\n\n{instruction}"
        if title is None:
            title = goal["title"]
    tool, = validated([{"name": "goal", "target": 1, **fields}]).tools
    timing_turn = turn
    timing = tool.get("timing", "").casefold()
    exchange = clarification_exchange(turn.history)
    if timing and timing not in turn.text.casefold() and exchange and timing in exchange[0]["text"].casefold():
        timing_turn = replace(turn, text=exchange[0]["text"])
    start = goal_start(timing_turn, tool, datetime.now(UTC))
    data = goal_data(tool, turn.zone, start, goal)
    if title is not None:
        data["title"] = title
    if goal.get("watch_provider") and data["frequency_minutes"]:
        data["watch_provider"] = goal["watch_provider"]
    required = changes.get("check_in", goal["check_in"] and not data["frequency_minutes"])
    if required and data["frequency_minutes"]:
        raise ValueError("Choose a fixed recurrence or a check-in that depends on your answer.")
    # A standalone check-in lives in the follow-up table. Preserve its chosen time when rewriting the task.
    due = goal["check_in_due"]
    preserve_due = (required and changes.get("query")) or (data["frequency_minutes"] and not data["scheduled_at"])
    if not start and preserve_due and due and datetime.fromisoformat(due) > datetime.now(UTC):
        data["scheduled_at"] = due
    if data["frequency_minutes"] and not data["scheduled_at"]:
        raise ValueError("What time should this repeating goal start?")
    return data, required


async def plan_edit(turn, goal, context, generate):
    data = {key: value for key, value in context.items() if key != "goals"}
    data["selected_goal"] = describe(goal, 1, None) | {"task": goal["prompt"]}
    data["upcoming_days"] = scope_prompt(turn.text, turn.zone, turn.started, "")["upcoming_days"]
    instructions = EDIT_GOAL
    if data.get("format_only"):
        instructions += ('\nThis is a format-only change. Return exactly {"format_instruction":"..."} with '
                         "the requested format, layout, and delivery details copied from the user's request.")
    for _ in range(2):
        changes = json_object(await generate(model_messages(instructions, data), 260) or "")
        if changes.keys() == {"question"}:
            question = changes["question"]
            if isinstance(question, str) and 0 < len(question.strip()) <= 480 and question.strip().endswith("?"):
                return question.strip()
        try:
            if data.get("format_only") and changes.keys() != {"format_instruction"}:
                raise ValueError("Only format_instruction may change for this request.")
            if (isinstance(start := changes.get("start"), str) and len(start) > 10
                    and (weekday := weekday_selector({"query": changes.get("timing")}))):
                day = with_weekday({}, weekday, turn.zone, turn.started)["start_date"]
                changes = changes | {"start": day + start[10:]}
            return changes, changes_for(turn, goal, changes)
        except ValueError as error:
            instructions = f"{instructions}\nCorrect the previous edit {changes!r}. {error}"
    return None
