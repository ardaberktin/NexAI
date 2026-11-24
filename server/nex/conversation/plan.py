"""Plan each turn before reading any tool results. Model outputs are untrusted."""
import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from ..accounts.intent import validate_scope
from ..activities.recognition import TITLES
from ..coding.intent import pasted_python
from ..conversation_history import MAX_EXCHANGES, prompt_history
from ..daybook import DayContext
from ..goal_schedule import FREQUENCIES
from ..goal_writing import json_object, model_messages
from ..model_context import ModelMessages, constrained_messages
from ..news_sources import validate_news_date
from ..recall_dates import time_zone
from ..store import excerpt
from ..tool_dates import absolute_day, clock_bounds, date_selection
from ..weather_intent import weather_city
from ..web_intent import literal_web_topic
from .calendar_scope import (
    CALENDAR_READS,
    normalize_calendar_dates,
    repair_calendar_scopes,
)
from .catalog import (
    CATALOG,
    CHOOSE_LANGUAGE,
    OPTIONS,
    PLANNING_TOOLS,
    PUBLIC,
    REVIEW_GOAL_EDIT,
    WEATHER_SCOPE,
    planning_instructions,
    schedule_instructions,
)
from .clock import DeviceClock
from .clock_tool import PLAN_CLOCK
from .document_routing import (
    CHOOSE_DOCUMENT_ACTION,
    DOCUMENT_ACTION_SCHEMA,
    PDF_READ_TOOLS,
    PDF_RECOVERY_SCHEMA,
    PLAN_PDF,
    PLAN_PDF_GOAL,
    REVIEW_PDF_DELIVERY,
    normalize_source_url,
    repair_document_search,
)
from .goal_context import goal_references, pending_pdf_action
from .goal_management_prompts import CHOOSE_DELETION_SCOPE, OPERATIONS
from .goal_plans import (
    GoalPlanError,
    choose_goal_target,
    review_goal_plan,
    validate_goal_action,
)
from .mail_watch_plan import PLAN_MAIL_WATCH, prepare_mail_watch
from .planning_schema import plan_schema
from .routing_prompt import Route, route_turn

LOG = logging.getLogger(__name__)
SCHEDULE_TOOLS = {"goal", "manage_goal", "agenda", "calendar", "ticktick", "gmail"}
PREFERENCE_TOOLS = {"save_memory", "weather_city"}


@dataclass
class Turn:
    id: str
    device: str
    text: str
    preference: str
    zone: str
    started: datetime = field(default_factory=lambda: datetime.now(UTC))
    history: tuple = ()
    journal: DayContext | None = None
    spoken: bool = False
    exclude: str | None = None
    resolved_language: str | None = None
    device_clock: DeviceClock | None = None
    legacy_mail: dict | None = None
    recalled_chat: dict | None = None

    @property
    def language(self):
        return self.preference if self.preference in ("en", "tr") else self.resolved_language or "en"

    def prompt(self, *, planning=False):
        local = self.device_clock.local_time() if self.device_clock else self.started.astimezone(time_zone(self.zone))
        data = {
            "local_time": local.isoformat(timespec="minutes"),
            "local_time_display": local.strftime("%I:%M %p").lstrip("0"),
            "local_time_zone": self.device_clock.time_zone if self.device_clock else self.zone,
            "local_date": local.date().isoformat(), "tomorrow_date": (local.date() + timedelta(days=1)).isoformat(),
            "language": self.language, "selected_day": self.journal.day if self.journal else None,
            "history": prompt_history(self.history, include_goal_receipts=not planning),
            "question": self.text,
        }
        if len(self.text) > 3600 and (program := pasted_python(self.text)):
            data["question"] = excerpt(self.text, 1800)
            data["pasted_program"] = {"preview_only": True, "source_bytes": len(program.source.encode())}
        if references := goal_references(self.history):
            data["goal_references"] = [
                {"target": index, **description}
                for index, (_, description) in enumerate(references, start=1)
            ]
        if self.legacy_mail:
            data["previous_mail_filter"] = self.legacy_mail
        if self.recalled_chat is not None:
            data["recalled_chat"] = self.recalled_chat
        return data


@dataclass(frozen=True)
class Plan:
    tools: tuple
    error: str = ""
    clarify: bool = False

    @property
    def private(self):
        return any(tool["name"] not in PUBLIC for tool in self.tools)

    @property
    def location(self):
        return any(tool["name"] == "weather" and not tool.get("query") for tool in self.tools)

    @property
    def screen(self):
        return any(tool["name"] == "screen" for tool in self.tools)


def validated(tools):
    if not isinstance(tools, list) or len(tools) > 4:
        raise ValueError("Expected up to four tools.")
    names = set()
    for tool in tools:
        name = tool.get("name") if isinstance(tool, dict) else None
        if not isinstance(name, str) or name not in CATALOG:
            raise ValueError('Use objects with a known tool name, such as {"name":"agenda","query":"today"}.')
        if name in names:
            raise ValueError(f"Select {name} only once.")
        allowed = {"name"} | OPTIONS.get(name, set())
        if name != "clock":
            allowed.add("query")
        if tool.keys() - allowed:
            raise ValueError(f"{name} only accepts these fields: {', '.join(sorted(allowed))}.")
        names.add(name)
        for key, value in tool.items():
            if key in {"unread", "lists", "priority", "access_check", "exact", "check_in", "format_only"}:
                valid = type(value) is bool
            elif key == "terms":
                valid = (isinstance(value, list) and len(value) <= 3
                         and all(isinstance(term, str) and 1 <= len(term) <= 110 for term in value))
            elif key == "count":
                valid = type(value) is int and 1 <= value <= 8
            elif key == "frequency":
                valid = type(value) is int and value in FREQUENCIES
            elif key == "target":
                valid = type(value) is int and 1 <= value <= MAX_EXCHANGES
            elif key == "duration_seconds":
                valid = type(value) is int and 0 < value <= 2**31 - 1
            elif key == "label":
                valid = isinstance(value, str) and len(value) <= 120
            else:
                valid = isinstance(value, str) and len(value) <= (1200 if name == "goal" else 420)
            if not valid:
                raise ValueError("Invalid tool argument.")
        if name == "weather" and tool.get("period", "current") not in {"current", "past", "forecast"}:
            raise ValueError("Unknown weather period.")
        if name in {"memory", "save_memory"} and tool.get("file", "memory") not in (
                {"memory", "soul"} if name == "save_memory" else {"now", "memory", "soul"}):
            raise ValueError("Unknown memory file.")
        needs_query = (name == "web" and "date" not in tool) or name == "save_memory" or (
            name == "goal" and ("target" not in tool or "query" in tool))
        if needs_query and not tool.get("query", "").strip():
            raise ValueError(f"{name} requires a nonempty query.")
        if "goal_query" in tool and not tool["goal_query"].strip():
            raise ValueError("Name or describe the goal to attach the PDF to, or omit goal_query.")
        if "watch_provider" in tool and tool["watch_provider"] != "gmail":
            raise ValueError("Choose a supported watch provider.")
        if name == "weather_city" and (tool.get("operation") not in {"set", "clear"} or
                                       bool(tool.get("query")) != (tool["operation"] == "set")):
            raise ValueError("Choose set with the named city or clear without a city.")
        if name == "web" and "date" in tool:
            validate_news_date(tool["date"])
        if name in {"weather", "daybook", "activities"} and "date" in tool:
            date.fromisoformat(tool["date"])
        if "start_date" in tool or "end_date" in tool:
            date_selection(tool)
        if name == "past_questions" and "before" in tool and tool["before"] != "last_result":
            raise ValueError("Set before to last_result for an earlier quote.")
        if name == "activities":
            if "category" in tool and tool["category"] not in TITLES:
                raise ValueError("Choose a supported activity category.")
            if "mode" in tool and tool["mode"] not in {"current", "range", "latest"}:
                raise ValueError("Select current, range, or latest for activity scope.")
            times = clock_bounds(tool, "UTC")
            dates = date_selection(tool) or tool.get("date")
            if (times and dates) or (tool.get("mode") == "range" and not (dates or times)):
                raise ValueError("Select one date or time range for activities.")
            if tool.get("mode") == "current" and (times or dates):
                raise ValueError("Current activity uses fresh recordings. Use range for a date or time window.")
        if name == "manage_goal" and ("query" in tool or
                ("operation" in tool and tool["operation"] not in OPERATIONS) or
                ("format_only" in tool and tool.get("operation") != "edit")):
            raise ValueError("Use a supported manage_goal operation. format_only applies to an edit.")
    if len(names) > 1 and names.intersection({"screen", "goal", "manage_goal", "save_memory", "weather_city", "clock"}):
        raise ValueError("Request a change or a screen inspection on its own.")
    return Plan(tuple(tools))


def validate_context(turn, plan):
    writes_pdf = any(tool["name"] == "create_pdf" for tool in plan.tools)
    for tool in plan.tools:
        if (writes_pdf and tool["name"] == "web" and (query := tool.get("query", "").strip())
                and not literal_web_topic(turn.text, query)):
            raise ValueError("Copy a public topic or the supplied public URL verbatim into the web query.")
        if tool["name"] in CALENDAR_READS:
            validate_scope(tool, turn.zone, turn.started)
        if tool["name"] == "past_questions" and "count" not in tool:
            raise ValueError("Select how many past questions to quote with count, from 1 to 8.")
        if tool["name"] == "activities" and "mode" not in tool:
            raise ValueError("Select an activity mode: current, range or latest.")
        if tool["name"] == "weather":
            period = tool.get("period")
            if period not in {"current", "past", "forecast"}:
                raise ValueError("Weather requires a period field: current, past, or forecast.")
            if period != "current":
                date.fromisoformat(tool.get("date", ""))
        if tool["name"] in {"weather", "weather_city"} and (city := tool.get("query", "")):
            utterances = [turn.text, *(item["text"] for item in turn.history if item["kind"] == "question")]
            named = re.compile(r"(?<!\w)" + re.escape(city.strip()) + r"(?!\w)", re.IGNORECASE)
            if not weather_city(city) or not any(named.search(text) for text in utterances):
                raise ValueError("Omit query for local weather. Only copy a city named by the user.")
    if turn.journal and any(tool["name"] not in {"daybook", "create_pdf"} for tool in plan.tools):
        raise ValueError("Use daybook for journal facts and create_pdf for a requested file.")


async def choose(turn, generate, *, recall=None):
    data = turn.prompt(planning=True) | {"language": turn.preference}
    plan = await select_tools(turn, generate, data, recall=recall)
    if turn.preference == "auto" and turn.resolved_language is None and not plan.error:
        language_data = {"history": data["history"][-4:], "latest_message": data["question"]}
        raw = await generate(model_messages(CHOOSE_LANGUAGE, language_data), 8)
        selected = raw.strip().lower().rstrip(".") if isinstance(raw, str) else None
        if selected in ("en", "tr"):
            turn.resolved_language = selected
    return plan


async def route_with_context(turn, generate, data, recall):
    if turn.journal or (not turn.spoken and pasted_python(turn.text)):
        return Route("general")
    route = await route_turn(turn, generate, data, allow_recall=recall is not None)
    if route.recall_query is not None:
        turn.recalled_chat = await asyncio.to_thread(recall, route.recall_query)
        data["recalled_chat"] = turn.recalled_chat
        route = await route_turn(turn, generate, data)
        if route.exchange is not None:
            match = turn.recalled_chat["exchanges"][route.exchange - 1]
            turn.recalled_chat = data["recalled_chat"] = {"status": "matched", "exchanges": [match]}
    return route


async def select_tools(turn, generate, data, *, recall=None):
    route = await route_with_context(turn, generate, data, recall)
    label = route.label
    if label in {"answer", "clarify"}:
        return Plan((), clarify=label == "clarify")
    if label == "manage" and pending_pdf_action(turn.history):
        return await select_document(turn, generate, data)
    if label == "delete" and not turn.journal:
        request = {"latest_message": turn.text, "history": data["history"][-4:]}
        raw = await generate(model_messages(CHOOSE_DELETION_SCOPE, request), 8)
        scope = raw.strip().upper().rstrip(".") if isinstance(raw, str) else ""
        if scope == "PDF":
            return Plan(({"name": "manage_goal", "operation": "remove_pdf"},))
        if scope == "OTHER":
            return Plan((), clarify=True)
    if not turn.journal and (label == "manage" or label in OPERATIONS):
        tool = {"name": "manage_goal"}
        if label in OPERATIONS:
            tool["operation"] = label
        return Plan((tool,))
    if not turn.journal and label in {"schedule", "recurring", "check_in"}:
        return await select_schedule(turn, generate, data, goal_type=label)
    if not turn.journal and label == "mail_watch":
        context = schedule_context(turn, data) | {"mail_watch": True}
        watch = {key: context[key] for key in ("local_time", "goal_time_zone", "question")}
        messages = model_messages(PLAN_MAIL_WATCH, watch, schema=plan_schema({"goal"}, goal_type=label))
        return await generate_plan(turn, generate, messages, goal_review=context, available={"goal"})
    if not turn.journal and label == "clock":
        return await generate_plan(turn, generate, model_messages(PLAN_CLOCK, data), available={"clock"})
    if label == "document" and not turn.journal:
        return await select_document(turn, generate, data)
    names = {"daybook", "create_pdf"} if turn.journal else route.tools or PLANNING_TOOLS
    setting_change = bool(route.tools & PREFERENCE_TOOLS)
    if setting_change:
        names = names | PREFERENCE_TOOLS
    instructions = planning_instructions(names, selected=bool(route.tools))
    if setting_change:
        instructions += ("\nChoose at most one change. Personal facts and preferences go to save_memory. "
                         "Changing or forgetting the forecast city goes to weather_city. Act only on an explicit request.")
    if turn.journal:
        instructions += "\nThe user selected a journal entry. Use only daybook for facts about this selection."
        data["selected_highlight"] = turn.journal.focus
    schema = plan_schema(names, minimum=1 if route.tools and not setting_change else 0,
                         maximum=1 if setting_change else None)
    plan = await generate_plan(turn, generate, model_messages(instructions, data, schema=schema),
                               available=names if route.tools else None)
    names = {tool["name"] for tool in plan.tools}
    if data["history"] and "weather" in names and "agenda" not in names:
        raw = await generate(model_messages(WEATHER_SCOPE, data), 12)
        verdict = raw.strip().upper().rstrip(".") if isinstance(raw, str) else ""
        if verdict == "NO_TOOLS":
            return Plan(())
        if verdict == "REPLAN":
            data = data | {"history": []}
            fallback = planning_instructions(PLANNING_TOOLS)
            plan = await generate_plan(turn, generate, model_messages(fallback, data, schema=plan_schema(PLANNING_TOOLS)))
    proposal = next((tool for tool in plan.tools if tool["name"] in {"agenda", "goal"}), None)
    if (proposal and (proposal["name"] == "goal" or not label)
            and "create_pdf" not in names and not turn.journal):
        return await select_schedule(turn, generate, data, proposal, plan.tools)
    return plan


async def select_document(turn, generate, data):
    context = schedule_context(turn, data)
    if pending := pending_pdf_action(turn.history):
        context["pending_pdf_action"] = pending
    raw = await generate(model_messages(CHOOSE_DOCUMENT_ACTION, context, schema=DOCUMENT_ACTION_SCHEMA), 72)
    raw = raw if isinstance(raw, str) else ""
    decision = json_object(raw)
    action = decision.get("action", raw.strip().upper().rstrip("."))
    if action == "NOW":
        sources = decision.get("sources")
        available = ({"create_pdf"} | {name for name in PDF_READ_TOOLS if name in sources}
                     if isinstance(sources, list) else PDF_READ_TOOLS)
        descriptions = "\n".join(
            f"{name}: {description}" for name, description in CATALOG.items() if name in available
        )
        messages = model_messages(f"{PLAN_PDF}\n{descriptions}", data, schema=plan_schema(available, minimum=1))
        return await generate_plan(turn, generate, messages, available=available, required="create_pdf")
    if action in {"REPEAT", "ONCE"}:
        context["document_delivery"] = action
        instructions = PLAN_PDF_GOAL + f"\nThe selected delivery mode is {action}."
        schema = plan_schema({"goal"}, goal_type="recurring" if action == "REPEAT" else None)
        plan = await generate_plan(turn, generate, model_messages(instructions, context, schema=schema),
                                   available={"goal"}, goal_review=context)
        if not plan.error:
            return plan if plan.tools else Plan((), clarify=True)
        prompt = model_messages(REVIEW_PDF_DELIVERY, context, schema=PDF_RECOVERY_SCHEMA)
        action = json_object(await generate(prompt, 35) or "").get("action")
        if action not in {"FORMAT", "REMOVE", "CLARIFY"}:
            return plan
    if action == "REMOVE":
        return Plan(({"name": "manage_goal", "operation": "remove_pdf"},))
    if action in {"FORMAT", "EDIT"}:
        return Plan(({"name": "manage_goal", "operation": "edit", "format_only": action == "FORMAT"},))
    if action in {"CLARIFY", "ADVICE", "SKIP"}:
        return Plan((), clarify=action == "CLARIFY")
    return Plan((), "model_unavailable")


async def select_schedule(turn, generate, data, proposal=None, tools=(), *, goal_type=None):
    context = schedule_context(turn, data)
    instructions = schedule_instructions(data["local_time"])
    can_edit_goal = context["goal_references"] and (proposal is None or proposal["name"] == "goal")
    if can_edit_goal and (target := await choose_goal_target(context, generate)):
        context["edit_target"] = target
        instructions = REVIEW_GOAL_EDIT
    review = {
        **context, "first_choice": proposal["name"] if proposal else None, "proposal": proposal,
        "related_tools": [tool for tool in tools if tool is not proposal],
    }
    if proposal and proposal["name"] == "agenda" and date_selection(proposal):
        review["agenda_dates"] = {key: proposal[key] for key in ("start_date", "end_date") if key in proposal}
    selected_type = goal_type if "edit_target" not in context else None
    schema = plan_schema(SCHEDULE_TOOLS | {tool["name"] for tool in tools}, goal_type=selected_type)
    return await generate_plan(turn, generate, model_messages(instructions, review, schema=schema), goal_review=context)


def schedule_context(turn, data):
    history = prompt_history(turn.history, include_tools=False, include_goal_receipts=False)[-4:] if data["history"] else []
    context = {"local_time": data["local_time"], "goal_time_zone": turn.zone, "tomorrow_date": data["tomorrow_date"],
               "history": history, "goal_references": data.get("goal_references", []), "question": data["question"]}
    if "recalled_chat" in data:
        context["recalled_chat"] = data["recalled_chat"]
    return context


async def generate_plan(turn, generate, base, *, goal_review=None, available=None, required=None):
    if available is not None and not getattr(base, "response_format", None):
        base = constrained_messages(base, plan_schema(available))
    messages = base
    invalid_goal = False
    for _ in range(2):
        raw = await generate(messages, 260)
        if not isinstance(raw, str) or not raw.strip():
            break
        try:
            tools = json_object(raw).get("tools")
            if isinstance(tools, list) and len(tools) <= 4:
                tools = [normalize_plan_tool(turn, tool) for tool in tools]
                tools = [tool for index, tool in enumerate(tools) if tool not in tools[:index]]
            if required == "create_pdf":
                tools = normalize_source_url(tools, turn.text)
            if goal_review and goal_review.get("mail_watch"):
                tools = prepare_mail_watch(tools, turn)
            plan = validated(tools)
            if available is not None and any(tool["name"] not in available for tool in plan.tools):
                raise ValueError("Use one of the tools described in this plan or return an empty tools array.")
            if required and plan.tools and all(tool["name"] != required for tool in plan.tools):
                raise ValueError(f"Include {required} with the read tools needed to complete the request.")
            validate_goal_action(plan.tools, goal_review)
            tools = await repair_calendar_scopes(turn, plan.tools, generate)
            tools = await repair_document_search(turn, tools, generate)
            plan = validated(tools)
            validate_context(turn, plan)
            if goal_review is not None:
                for tool in plan.tools:
                    if tool["name"] == "goal":
                        await review_goal_plan(turn, tool, generate, goal_review)
            return plan
        except ValueError as error:
            invalid_goal |= isinstance(error, GoalPlanError)
            LOG.debug("Nex tool plan failed validation (%s)", type(error).__name__)
            messages = ModelMessages(
                [*base, {"role": "assistant", "content": excerpt(raw, 400)},
                 {"role": "user", "content": f'{error} Return a JSON object with a "tools" array.'}],
                getattr(base, "response_format", None),
            )
    if invalid_goal:
        return Plan((), "invalid_goal_plan")
    return Plan((), "model_unavailable")


def normalize_plan_tool(turn, tool):
    if not isinstance(tool, dict):
        return tool
    tool = normalize_calendar_dates(tool, turn.zone, turn.started)
    if (tool.get("name") == "weather" and isinstance(place := tool.get("query"), str)
            and place.strip().casefold() in {"local", "here", "my location", "current location"}):
        tool = {key: value for key, value in tool.items() if key != "query"}
    if tool.get("name") in ("weather", "archive", "past_questions", "activities", "daybook"):
        fields = {key: absolute_day(tool[key], turn.zone, turn.started)
                  for key in ("date", "start_date", "end_date") if key in tool}
        tool = tool | fields
    return tool
