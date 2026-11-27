"""Select the settings an edit is allowed to supply before generating field values."""

from ..goal_writing import json_object, model_messages

EDIT_FIELDS = {
    "title": {"type": "string"}, "query": {"type": "string"}, "start": {"type": "string"},
    "timing": {"type": "string"}, "frequency": {"type": "integer"}, "check_in": {"type": "boolean"},
    "format_instruction": {"type": "string"},
}
CHOOSE_FIELDS = """Which settings of this selected goal should change? Return fields only.
query is the task or matching criteria. title is the displayed name. start moves its day or clock.
frequency changes the repetition interval. check_in changes whether it asks the user about the outcome.
format_instruction changes the output format. Never include fields just because the user wants to keep
them. Use the earlier clarification to identify the requested change when the current message names
one of the choices. If an essential detail for the edit is missing, return an empty list."""
FIELD_CHOICES = sorted(EDIT_FIELDS.keys() - {"timing"})
FIELD_SCHEMA = {"type": "object", "required": ["fields"], "additionalProperties": False,
                "properties": {"fields": {"type": "array", "items": {"enum": FIELD_CHOICES}, "maxItems": len(FIELD_CHOICES)}}}


async def edit_schema(data, generate):
    request = {"selected_goal": data["selected_goal"], "clarification": data.get("clarification"),
               "latest_edit_request": data["latest_edit_request"]}
    raw = await generate(model_messages(CHOOSE_FIELDS, request, schema=FIELD_SCHEMA), 65)
    fields = json_object(raw or "").get("fields")
    if not isinstance(fields, list) or not all(field in FIELD_CHOICES for field in fields):
        return None
    properties = {field: EDIT_FIELDS[field] for field in fields}
    if "start" in properties:
        properties["timing"] = EDIT_FIELDS["timing"]
        properties["start"] = {"type": "string", "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}$"}
    if not properties:
        properties["question"] = {"type": "string"}
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}
