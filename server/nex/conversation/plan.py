"""Plan each turn before reading any tool results. Model outputs are untrusted."""
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from ..accounts.intent import validate_scope
from ..activities.recognition import TITLES
from ..coding.intent import pasted_python
from ..conversation_history import MAX_EXCHANGES, prompt_history
from ..daybook import DayContext
from ..goal_schedule import FREQUENCIES
from ..goal_writing import json_object, model_messages
from ..news_sources import validate_news_date
from ..recall_dates import time_zone
from ..store import excerpt
from ..tool_dates import clock_bounds, date_selection
from ..weather_intent import weather_city
from ..web_intent import literal_web_topic
from .calendar_scope import CALENDAR_READS, repair_calendar_scopes
from .catalog import (
    CATALOG,
    CHOOSE_LANGUAGE,
    CLARITY,
    INSTRUCTIONS,
    OPTIONS,
    PUBLIC,
    REVIEW_GOAL_EDIT,
    WEATHER_SCOPE,
    routing_examples,
    schedule_instructions,
)
from .clock import DeviceClock
from .clock_tool import PLAN_CLOCK
from .document_routing import (
    CHOOSE_DOCUMENT_ACTION,
    CHOOSE_PDF_CADENCE,
    PDF_READ_TOOLS,
    PLAN_PDF,
    PLAN_PDF_GOAL,
    normalize_source_url,
    repair_document_search,
)
from .goal_context import goal_references
from .goal_management_prompts import GOAL_INTENT, OPERATIONS
from .goal_plans import (
    GoalPlanError,
    choose_goal_target,
    review_goal_plan,
    validate_goal_action,
)
from .mail_watch_plan import PLAN_MAIL_WATCH, prepare_mail_watch, wants_mail_watch


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

    @property
    def language(self):
        return self.preference if self.preference in ("en", "tr") else self.resolved_language or "en"

    def prompt(self, *, planning=False):
        local = self.device_clock.local_time() if self.device_clock else self.started.astimezone(time_zone(self.zone))
        data = {
            "local_time": local.isoformat(timespec="minutes"),
            "local_time_display": local.strftime("%I:%M %p").lstrip("0"),
            "local_time_zone": self.device_clock.time_zone if self.device_clock else self.zone,
            "language": self.language, "selected_day": self.journal.day if self.journal else None,
            "history": prompt_history(self.history, include_goal_receipts=not planning),
            "question": excerpt(self.text, 3600),
        }
        if references := goal_references(self.history):
            data["goal_references"] = [
                {"target": index, **description}
                for index, (_, description) in enumerate(references, start=1)
            ]
        if self.legacy_mail:
            data["previous_mail_filter"] = self.legacy_mail
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


async def choose(turn, generate):
    data = turn.prompt(planning=True) | {"language": turn.preference}
    plan = await select_tools(turn, generate, data)
    if turn.preference == "auto" and not plan.error:
        language_data = {"history": data["history"][-4:], "latest_message": data["question"]}
        raw = await generate(model_messages(CHOOSE_LANGUAGE, language_data), 8)
        selected = raw.strip().lower().rstrip(".") if isinstance(raw, str) else None
        if selected in ("en", "tr"):
            turn.resolved_language = selected
    return plan


async def select_tools(turn, generate, data):
    label = ""
    if turn.spoken:
        check = model_messages(CLARITY, {"history": data["history"], "latest_utterance": data["question"]})
        verdict = await generate(check, 6)
        status = verdict.strip().upper().rstrip(".") if isinstance(verdict, str) else ""
        if status in {"SOCIAL", "UNCLEAR"}:
            return Plan((), clarify=status == "UNCLEAR")
        label = status.lower()
    if not turn.spoken and not turn.journal and not pasted_python(turn.text):
        request = {key: data[key] for key in ("local_time", "local_time_display", "local_time_zone")}
        request.update(history=data["history"][-4:], latest_message=data["question"])
        intent = await generate(model_messages(GOAL_INTENT, request), 6)
        label = intent.strip().lower().rstrip(".") if isinstance(intent, str) else ""
    if not turn.journal and (label == "manage" or label in OPERATIONS):
        tool = {"name": "manage_goal"}
        if label in OPERATIONS:
            tool["operation"] = label
        return Plan((tool,))
    if not turn.journal and label == "schedule":
        return await select_schedule(turn, generate, data)
    if not turn.journal and label == "clock":
        return await generate_plan(turn, generate, model_messages(PLAN_CLOCK, data), available={"clock"})
    if label == "document" and not turn.journal:
        return await select_document(turn, generate, data)
    clock = f"Reading from the asking device's clock: {data['local_time_display']} ({data['local_time']})."
    instructions = INSTRUCTIONS + routing_examples(data["local_time"]) + "\n" + clock
    if turn.journal:
        instructions += "\nThe user selected a journal entry. Use only daybook for facts about this selection."
        data["selected_highlight"] = turn.journal.focus
    plan = await generate_plan(turn, generate, model_messages(instructions, data))
    names = {tool["name"] for tool in plan.tools}
    if data["history"] and "weather" in names and "agenda" not in names:
        raw = await generate(model_messages(WEATHER_SCOPE, data), 12)
        verdict = raw.strip().upper().rstrip(".") if isinstance(raw, str) else ""
        if verdict == "NO_TOOLS":
            return Plan(())
        if verdict == "REPLAN":
            data = data | {"history": []}
            plan = await generate_plan(turn, generate, model_messages(instructions, data))
    proposal = next((tool for tool in plan.tools if tool["name"] in {"agenda", "goal"}), None)
    if proposal and all(tool["name"] != "create_pdf" for tool in plan.tools):
        return await select_schedule(turn, generate, data, proposal, plan.tools)
    return plan


async def select_document(turn, generate, data):
    context = schedule_context(turn, data)
    raw = await generate(model_messages(CHOOSE_DOCUMENT_ACTION, context), 12)
    action = raw.strip().upper().rstrip(".") if isinstance(raw, str) else ""
    if action in {"FORMAT", "EDIT"}:
        return Plan(({"name": "manage_goal", "operation": "edit", "format_only": action == "FORMAT"},))
    if action not in {"CREATE", "SCHEDULE"}:
        return Plan((), "" if action == "CLARIFY" else "model_unavailable")
    raw = await generate(model_messages(CHOOSE_PDF_CADENCE, context), 8)
    cadence = raw.strip().upper().rstrip(".") if isinstance(raw, str) else ""
    # A one-time PDF is immediate unless the delivery planner identified a future request.
    if action == "CREATE" and cadence in {"NOW", "ONCE"}:
        descriptions = "\n".join(
            f"{name}: {description}" for name, description in CATALOG.items() if name in PDF_READ_TOOLS
        )
        return await generate_plan(turn, generate, model_messages(f"{PLAN_PDF}\n{descriptions}", data),
                                   available=PDF_READ_TOOLS, required="create_pdf")
    if cadence in {"REPEAT", "ONCE"}:
        context["document_delivery"] = cadence
        instructions = PLAN_PDF_GOAL + f"\nThe selected delivery mode is {cadence}."
        return await generate_plan(turn, generate, model_messages(instructions, context),
                                   available={"goal"}, goal_review=context)
    return Plan((), "model_unavailable")


async def select_schedule(turn, generate, data, proposal=None, tools=()):
    context = schedule_context(turn, data)
    instructions = schedule_instructions(data["local_time"])
    can_edit_goal = context["goal_references"] and (proposal is None or proposal["name"] == "goal")
    if can_edit_goal and (target := await choose_goal_target(context, generate)):
        context["edit_target"] = target
        instructions = REVIEW_GOAL_EDIT
    if "edit_target" not in context and await wants_mail_watch(context, generate):
        context["mail_watch"] = True
        watch = {key: context[key] for key in ("local_time", "goal_time_zone", "question")}
        return await generate_plan(turn, generate, model_messages(PLAN_MAIL_WATCH, watch), goal_review=context)
    if proposal is None and "edit_target" not in context:
        context["require_goal"] = True
        instructions += "\nPlan future delivery using goal. Include the requested format and contents in its query."
    review = {
        **context, "first_choice": proposal["name"] if proposal else None, "proposal": proposal,
        "related_tools": [tool for tool in tools if tool is not proposal],
    }
    if proposal and proposal["name"] == "agenda" and date_selection(proposal):
        review["agenda_dates"] = {key: proposal[key] for key in ("start_date", "end_date") if key in proposal}
    return await generate_plan(turn, generate, model_messages(instructions, review), goal_review=context)


def schedule_context(turn, data):
    history = prompt_history(turn.history, include_tools=False, include_goal_receipts=False)[-4:] if data["history"] else []
    return {"local_time": data["local_time"], "goal_time_zone": turn.zone,
            "history": history, "goal_references": data.get("goal_references", []), "question": data["question"]}


async def generate_plan(turn, generate, base, *, goal_review=None, available=None, required=None):
    messages = base
    invalid_goal = False
    for _ in range(2):
        raw = await generate(messages, 260)
        if not isinstance(raw, str) or not raw.strip():
            break
        try:
            tools = json_object(raw).get("tools")
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
            messages = [*base, {"role": "assistant", "content": excerpt(raw, 400)},
                        {"role": "user", "content": f'{error} Return a JSON object with a "tools" array.'}]
    if invalid_goal:
        return Plan((), "invalid_goal_plan")
    return Plan((), "model_unavailable")
