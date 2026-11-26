"""Plan each turn before reading any tool results. Model outputs are untrusted."""
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from ..conversation_history import prompt_history
from ..daybook import DayContext
from ..goal_writing import json_object, model_messages
from ..recall_dates import time_zone
from ..response_language import reply_language
from ..store import excerpt
from ..weather_intent import weather_city
from .catalog import CATALOG, CLARITY, EXAMPLES, INSTRUCTIONS, OPTIONS, PUBLIC


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

    @property
    def language(self):
        return reply_language(self.text, self.preference)

    def prompt(self):
        return {"local_time": self.started.astimezone(time_zone(self.zone)).isoformat(timespec="minutes"),
                "history": prompt_history(self.history), "question": excerpt(self.text, 3600),
                "language": self.language, "selected_day": self.journal.day if self.journal else None}


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
        if not isinstance(tool, dict) or not isinstance(name := tool.get("name"), str) or name not in CATALOG:
            raise ValueError("Unknown tool.")
        if name in names or tool.keys() - ({"name", "query"} | OPTIONS.get(name, set())):
            raise ValueError("Unexpected tool arguments.")
        names.add(name)
        for key, value in tool.items():
            if key in {"unread", "lists", "priority", "access_check"}:
                valid = type(value) is bool
            elif key == "frequency":
                valid = type(value) is int and value in (0, 15, 60, 1440, 10080)
            else:
                valid = isinstance(value, str) and len(value) <= (1200 if name == "goal" else 420)
            if not valid:
                raise ValueError("Invalid tool argument.")
        if name == "weather" and tool.get("period", "current") not in {"current", "past", "forecast"}:
            raise ValueError("Unknown weather period.")
        if name in {"memory", "save_memory"} and tool.get("file", "memory") not in (
                {"memory", "soul"} if name == "save_memory" else {"now", "memory", "soul"}):
            raise ValueError("Unknown memory file.")
        if name in {"web", "save_memory", "goal"} and not tool.get("query", "").strip():
            raise ValueError(f"{name} requires a nonempty query.")
        if name == "weather_city" and (tool.get("operation") not in {"set", "clear"} or
                                       bool(tool.get("query")) != (tool["operation"] == "set")):
            raise ValueError("Choose set with the named city or clear without a city.")
        if name == "goal" and "start" in tool:
            datetime.fromisoformat(tool["start"])
        if name in {"weather", "daybook", "activities"} and "date" in tool:
            date.fromisoformat(tool["date"])
    if len(names) > 1 and names.intersection({"screen", "goal", "save_memory", "weather_city"}):
        raise ValueError("Request a change or a screen inspection on its own.")
    return Plan(tuple(tools))


def validate_context(turn, plan):
    for tool in plan.tools:
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
    if turn.journal and any(tool["name"] != "daybook" for tool in plan.tools):
        raise ValueError("Only daybook is available inside the selected journal entry.")


async def choose(turn, generate):
    data = turn.prompt()
    if turn.spoken:
        check = model_messages(CLARITY, {"history": data["history"], "latest_utterance": data["question"]})
        verdict = await generate(check, 6)
        status = verdict.strip().upper().rstrip(".") if isinstance(verdict, str) else ""
        if status in {"SOCIAL", "UNCLEAR"}:
            return Plan((), clarify=status == "UNCLEAR")
    instructions = INSTRUCTIONS + EXAMPLES
    if turn.journal:
        instructions += "\nThe user selected a journal entry. Use only daybook for facts about this selection."
        data["selected_highlight"] = turn.journal.focus
    base = model_messages(instructions, data)
    messages = base
    for _ in range(2):
        raw = await generate(messages, 260)
        if not isinstance(raw, str) or not raw.strip():
            break
        try:
            plan = validated(json_object(raw).get("tools"))
            validate_context(turn, plan)
            return plan
        except ValueError as error:
            messages = [*base, {"role": "assistant", "content": excerpt(raw, 400)},
                        {"role": "user", "content": f"{error} Return a valid JSON tools array."}]
    return Plan((), "I couldn’t plan this request with the local model. Please try again.")
