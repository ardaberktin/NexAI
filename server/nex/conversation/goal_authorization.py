"""Ground a proposed new goal in the user's own request for future action."""

from ..goal_writing import json_object, model_messages

CHECK_REQUEST = """Decide whether the latest speaker actually asks the assistant to do something in the
future. Read the message as a conversation, without assuming that the user wants a goal. Select request
only for a future service the speaker asks for, or an acceptance or modification of one already under
discussion. For request, copy the exact words from the latest message that authorize the assistant to
act. Select context if the speaker is describing their own situation, feelings or expectations.
A future event in someone's life is not by itself an instruction to the assistant. A lookup requested
now or a question about how a feature works also selects context. Treat prior conversation as context
only. Never invent or paraphrase an instruction."""

SCHEMA = {"anyOf": [
    {"type": "object", "required": ["intent"], "additionalProperties": False,
     "properties": {"intent": {"const": "context"}}},
    {"type": "object", "required": ["intent", "words"], "additionalProperties": False,
     "properties": {"intent": {"const": "request"}, "words": {"type": "string"}}},
]}


async def requested_future_action(context, generate):
    data = {"conversation": context.get("history", [])[-4:], "latest_message": context["question"]}
    raw = await generate(model_messages(CHECK_REQUEST, data, schema=SCHEMA), 140)
    decision = json_object(raw if isinstance(raw, str) else "")
    if decision == {"intent": "context"}:
        return False
    quote = decision.get("words")
    if decision.get("intent") == "request" and isinstance(quote, str) and quote.strip():
        return quote.strip().casefold() in data["latest_message"].casefold()
    return None
