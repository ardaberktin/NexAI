"""Shape a model plan before validating the proposed operation in application code."""

from .catalog import OPTIONS
from .goal_management_prompts import OPERATIONS

BOOLEAN = {"unread", "priority", "exact", "lists", "access_check", "check_in", "format_only"}
INTEGER = {"count", "duration_seconds", "frequency", "target"}
REQUIRED = {"weather": ("period", "date"), "save_memory": ("query",), "clock": ("operation",),
            "past_questions": ("count",), "activities": ("mode",), "weather_city": ("operation",)}
SCHEDULED_FIELDS = {"new_calendar": {"start_date", "end_date"}, "new_mail": set(), "create_pdf": set(),
                    "weather": set()}
VALUES = {
    ("weather", "period"): ["current", "forecast", "past"],
    ("weather_city", "operation"): ["set", "clear"],
    ("activities", "mode"): ["current", "latest", "range"],
    ("memory", "file"): ["memory", "now", "soul"],
    ("save_memory", "file"): ["memory", "soul"],
    ("past_questions", "count"): list(range(1, 9)),
    ("past_questions", "before"): ["last_result"],
    ("clock", "kind"): ["timer", "alarm"],
    ("clock", "operation"): ["create", "list", "cancel"],
    ("manage_goal", "operation"): sorted(OPERATIONS),
    ("goal", "frequency"): [0, 15, 60, 1440, 10080],
}


def argument_schema(name, field):
    if values := VALUES.get((name, field)):
        return {"enum": values}
    if field in BOOLEAN:
        return {"type": "boolean"}
    if field in INTEGER:
        return {"type": "integer"}
    if field == "terms":
        return {"type": "array", "items": {"type": "string"}, "maxItems": 3}
    return {"type": "string"}


def tool_object(name, scheduled, goal_type):
    options = SCHEDULED_FIELDS if scheduled and name in SCHEDULED_FIELDS else OPTIONS
    fields = set(options.get(name, ())) - {"watch_provider"}
    if name not in {"clock", "manage_goal", "screen", "past_questions"}:
        fields.add("query")
    properties = {"name": {"const": name}} | {field: argument_schema(name, field) for field in sorted(fields)}
    required = [field for field in REQUIRED.get(name, ()) if field in fields]
    if name == "goal" and goal_type == "recurring":
        properties.pop("timing", None)
        properties.pop("check_in", None)
        properties["frequency"] = {"enum": [15, 60, 1440, 10080]}
        required.extend(("query", "frequency", "start"))
    if name == "goal" and goal_type == "check_in":
        properties["check_in"] = {"const": True}
        properties.pop("frequency", None)
        required.extend(("query", "check_in"))
    if name == "goal" and goal_type == "mail_watch":
        properties["delay_minutes"] = {"type": "integer", "minimum": 1}
        properties["frequency"] = {"enum": [15, 60, 1440, 10080]}
        properties.pop("check_in", None)
        properties.pop("target", None)
        required.extend(("query", "frequency"))
    return {"type": "object", "properties": properties, "required": ["name", *required],
            "additionalProperties": False}


def plan_schema(names, *, scheduled=False, minimum=0, maximum=None, goal_type=None):
    limit = min(len(names), maximum or (5 if scheduled else 4))
    return {
        "type": "object", "required": ["tools"], "additionalProperties": False,
        "properties": {"tools": {"type": "array", "minItems": minimum, "maxItems": limit,
                                 "items": {"anyOf": [tool_object(name, scheduled, goal_type) for name in sorted(names)]}}},
    }
