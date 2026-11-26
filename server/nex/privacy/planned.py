"""Complete an approved plan, including requests queued by a previous release."""
from ..conversation.plan import Plan, validated
from ..conversation.sync import execute, make_turn, select
from ..daybook import DayContext


def approved_reply(assistant, identifier, device, data, asked_at, coordinates):
    context = DayContext(**data["journal"]) if data.get("journal") else None
    turn = make_turn(identifier, device, data["text"], data["language"], data["zone"], asked_at=asked_at,
                     history=data.get("history", ()), journal=context, exclude=data.get("exclude") or identifier)
    if "tools" in data:
        plan = Plan(validated(data["tools"]).tools, data.get("plan_error", ""), data.get("clarify", False))
    else:
        # Older requests could carry the resolved Gmail filter for a pronominal follow-up.
        if mail := data.get("mail"):
            turn.history = ({"kind": "question", "text": f"Read my Gmail about {mail.get('topic', '')}"},
                            {"kind": "answer", "text": "Private inbox request."})
        plan = select(assistant, turn)
    return execute(assistant, turn, plan, private=True, coordinates=coordinates)
