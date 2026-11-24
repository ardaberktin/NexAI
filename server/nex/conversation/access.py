"""Keep disclosure and device permissions independent of the model's tool choices."""
from ..coding.intent import pasted_python
from ..privacy.policy import request_kind


def needs_unlock(turn, plan, force=False):
    # Existing disclosure rules are a floor even if a model misses a private intent.
    if plan.error or plan.private or force or turn.journal is not None:
        return True
    # Program bodies are opaque data once the planner has selected only the offline sandbox.
    if len(plan.tools) == 1 and plan.tools[0]["name"] == "python" and pasted_python(turn.text):
        return False
    return request_kind(turn.text, turn.zone, history=turn.history) != "public"


def can_preflight(plan):
    return bool(plan.tools) and all(tool["name"] in {"web", "python"} for tool in plan.tools)
