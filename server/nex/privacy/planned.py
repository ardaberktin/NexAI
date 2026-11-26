"""Complete an approved plan, including requests queued by a previous release."""
from ..conversation.clock import DeviceClock
from ..conversation.plan import Plan, validated
from ..conversation.sync import execute, make_turn, select
from ..daybook import DayContext


def approved_reply(assistant, identifier, device, data, asked_at, coordinates):
    context = DayContext(**data["journal"]) if data.get("journal") else None
    turn = make_turn(identifier, device, data["text"], data["language"], data["zone"], asked_at=asked_at,
                     history=data.get("history", ()), journal=context, exclude=data.get("exclude") or identifier,
                     device_clock=DeviceClock.read(data.get("device_clock")), recalled_chat=data.get("recalled_chat"),
                     attachments=tuple(data.get("attachments") or ()), agent_loop=data.get("agent_loop") is True)
    turn.private_chat = data.get("private_chat") is True
    if "tools" in data:
        plan = Plan(validated(data["tools"]).tools, data.get("plan_error", ""), data.get("clarify", False))
    else:
        # Older requests could carry the resolved Gmail filter for a pronominal follow-up.
        turn.legacy_mail = data.get("mail")
        plan = select(assistant, turn)
    return execute(assistant, turn, plan, private=True, coordinates=coordinates)
