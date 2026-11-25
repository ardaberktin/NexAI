"""Resolve a private goal action and commit only the verified change."""

import asyncio

from ..conversation_history import prompt_history
from ..goal_writing import json_object, model_messages, yes_no
from ..goals import Goals
from ..workspace_features import FeatureDisabled
from ..workspace_schema import WorkspaceConflict, WorkspaceMissing
from .goal_context import clarification_exchange, pdf_removal_history
from .goal_edit import plan_edit
from .goal_lookup import (
    PAGE_SIZE,
    clarify_target,
    describe,
    exact_title_targets,
    listed_target,
    named_targets,
    outcome,
    pdf_clarification_targets,
    pdf_target_schema,
    saved_goals,
    shortlist,
)
from .goal_management_prompts import (
    MANAGE_GOALS,
    OPERATIONS,
    SELECT_GOAL,
    SELECT_PDF_GOAL,
    VERIFY_CHANGE,
    VERIFY_NAMED_DELETE,
    VERIFY_PDF_CHANGE,
    VERIFY_PDF_CLARIFICATION,
    VERIFY_PDF_TARGET,
)
from .goal_pdf_removal import remove_pdf
from .goal_tools import goal_receipt
from .tools import Evidence


def apply_change(store, turn, goal, action, edit, *, format_instruction=None):
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
        if edit is None:
            saved = goals.set_pdf_format(identifier, revision, format_instruction)
        else:
            data, required = edit
            request = turn.text if data["prompt"] != goal["prompt"] else goal["request"] or goal["prompt"]
            saved = goals.save(identifier, data, request=request, language=turn.language, check_in=required)
        receipt = goal_receipt(store, saved, updated=True) | {"action": "updated"}
        if format_instruction:
            receipt["format_instruction"] = format_instruction.strip()
    return Evidence(receipt, engine="goal_" + receipt["action"], goal_id=identifier)


def parse_decision(result, candidates):
    action = result.get("action")
    if result == {"action": "none"}:
        return outcome("no_change_requested")
    if action == "clarify" and result.keys() == {"action", "question"}:
        question = result["question"]
        if isinstance(question, str) and len(question.strip()) <= 480 and "?" in question:
            return outcome("clarification_needed", suggested_question=question.strip())
    keys = {"action", "target", "title"}
    if not isinstance(action, str) or action not in OPERATIONS or result.keys() != keys:
        raise ValueError("Choose one supported action and only its arguments.")
    return listed_target(result, candidates)


def management_context(turn, operation, format_only, exchange):
    prompt = turn.prompt()
    context = {key: prompt[key] for key in ("question", "local_time", "language", "history")}
    if turn.recalled_chat is not None:
        context["recalled_chat"] = turn.recalled_chat
    context["history"] = prompt_history(turn.history, include_tools=False)[-4:]
    context["goal_time_zone"] = turn.zone
    if exchange:
        history = pdf_removal_history(turn.history) if operation == "remove_pdf" else exchange
        context["clarification"] = prompt_history(history, include_tools=False)
    if operation:
        context["requested_action"] = operation
    if format_only:
        context["format_only"] = True
    return context


async def manage_goal(store, turn, generate, operation=None, *, format_only=False):
    if operation == "keep_settings":
        return outcome("no_change_requested")
    format_only = format_only or operation == "remove_pdf"
    try:
        goals = await asyncio.to_thread(saved_goals, store)
        if not goals:
            return outcome("no_saved_goals")
        exchange = clarification_exchange(turn.history)
        context = management_context(turn, operation, format_only, exchange)
        recent = next((item["goal_id"] for item in reversed(turn.history) if item.get("goal_id")), None)
        descriptions = [describe(goal, i, recent) for i, goal in enumerate(goals, 1)]
        offered = exchange[-1].get("goal_choices") if format_only and exchange else None
        candidates = descriptions
        if offered:
            candidates = [row for row in descriptions if goals[row["target"] - 1]["id"] in offered]
        if operation == "remove_pdf":
            return await remove_pdf(store, goals, candidates, context, generate)
        exact_titles = exact_title_targets(turn.text, descriptions) if format_only else set()
        if format_only and not exact_titles:
            exact_titles = set(named_targets(turn.text, descriptions))
        if offered:
            descriptions = candidates
            if not descriptions or (exact_titles and exact_titles.isdisjoint(row["target"] for row in descriptions)):
                return clarify_target(descriptions, goals)
        if format_only and not offered and len(exact_titles) == 1:
            descriptions = [row for row in descriptions if row["target"] in exact_titles]
        named = [] if format_only else named_targets(turn.text, descriptions)
        if operation in {"delete", "complete", "keep"} and named:
            descriptions = [row for row in descriptions if row["target"] in named]
        check_matches = operation == "delete" and len(named) != 1
        check_pdf_matches = format_only and not offered and len(descriptions) > 1
        matches = await shortlist(descriptions, context, generate, verify_match=check_matches or check_pdf_matches)
        if format_only and exact_titles and exact_titles.isdisjoint(row["target"] for row in matches):
            matches.extend(row for row in descriptions if row["target"] in exact_titles)
        if format_only and len(matches) < len(descriptions) <= PAGE_SIZE:
            if matches:
                context["suggested_targets"] = [row["target"] for row in matches]
            matches = descriptions
        if not matches:
            return outcome("clarification_needed" if format_only else "no_matching_goal")
        if len(matches) > PAGE_SIZE:
            if format_only:
                return clarify_target(matches, goals)
            return outcome("ambiguous_goal")
        if check_matches and len(matches) > 1:
            return outcome("ambiguous_goal", candidate_titles=[row["title"] for row in matches])
        pdf_candidates = descriptions if format_only and len(descriptions) <= PAGE_SIZE else matches
        return await decide_change(store, turn, generate, goals, context | {"goals": matches}, named, pdf_candidates,
                                   exact_titles=exact_titles)
    except (FeatureDisabled, ValueError, WorkspaceConflict, WorkspaceMissing) as error:
        return outcome("change_rejected", detail=str(error))


async def decide_change(store, turn, generate, goals, context, named, pdf_candidates, *, exact_titles):
    operation = context.get("requested_action")
    format_only = context.get("format_only", False)
    guidance = SELECT_PDF_GOAL if format_only else SELECT_GOAL if operation else MANAGE_GOALS
    schema = pdf_target_schema(context["goals"]) if format_only else None
    selection = dict(context)
    if operation and not (format_only and context.get("clarification")):
        selection.pop("history")
        selection["goals"] = [{k: v for k, v in row.items() if k != "last_discussed_goal"} for row in context["goals"]]
    instructions = guidance
    for _ in range(3):
        result = json_object(await generate(model_messages(instructions, selection, schema=schema), 280) or "")
        if (result.get("action") in ("use_history", "clarify")
                and "history" not in selection and context["history"] and not named):
            selection = context
            continue
        if result == {"action": "use_history"}:
            return outcome("ambiguous_goal")
        if operation and result.keys() == {"target", "title"}:
            result["action"] = operation
        try:
            if format_only and result.get("action") == "clarify":
                return clarify_target(pdf_clarification_targets(result, context["goals"]), goals)
            target = parse_decision(result, context["goals"])
            if isinstance(target, Evidence):
                return target
            if exact_titles and target not in exact_titles:
                raise ValueError("The selected goal conflicts with the saved title named in the latest message.")
            goal = goals[target - 1]
            action = result["action"]
            if (operation and operation != action) or action == "remove_pdf":
                raise ValueError("Return only the selected target and its copied title, none, or clarify.")
            if action in {"delete", "complete", "keep"} and named and target not in named:
                raise ValueError("Use the goal named in the latest message or clarify.")
            if (format_only and action == "edit" and exact_titles != {target}
                    and not await verified_pdf_target(context, pdf_candidates, target, generate)):
                choices = context["goals"] if len(context["goals"]) > 1 else pdf_candidates
                return clarify_target(choices, goals)
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
            fields = result.get("changes", {})
            format_instruction = fields.get("format_instruction") if fields.keys() == {"format_instruction"} else None
            return await asyncio.to_thread(apply_change, store, turn, goal, action, edit,
                                           format_instruction=format_instruction)
        if verdict is None and "history" not in selection and context["history"] and not named:
            selection = context
            continue
        instructions = f"{guidance}\nThe verifier rejected {result!r}. Correct the action or target, or clarify."
    return clarify_target(context["goals"], goals) if format_only else outcome("unverified_change")


async def verified_pdf_target(context, candidates, target, generate):
    if len(candidates) <= 1:
        return True
    selected = next(row for row in candidates if row["target"] == target)
    data = {"latest_message": context["question"], "selected_goal": selected,
            "other_candidates": [row for row in candidates if row["target"] != target]}
    if clarification := context.get("clarification"):
        data["preceding_clarification"] = clarification
    return yes_no(await generate(model_messages(VERIFY_PDF_TARGET, data), 8)) is True


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
    if context.get("format_only"):
        if not (clarification := context.get("clarification")):
            data = {"selected_goal": goal["title"], "latest_message": context["question"]}
            return yes_no(await generate(model_messages(VERIFY_PDF_CHANGE, data), 8)) is True
        review["preceding_clarification"] = clarification
        return yes_no(await generate(model_messages(VERIFY_PDF_CLARIFICATION, review), 8)) is True
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
