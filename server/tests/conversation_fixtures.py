"""Script the model boundary while exercising the production planner, tools and storage."""
import json
from time import time
from unittest.mock import Mock

from nex.conversation.catalog import (
    CHOOSE_GOAL_TARGET,
    CHOOSE_LANGUAGE,
    CLARITY,
    INSTRUCTIONS,
    REVIEW_GOAL_EDIT,
    REVIEW_SCHEDULE,
    VERIFY_GOAL_PLAN,
    WEATHER_SCOPE,
)
from nex.conversation.clock_tool import PLAN_CLOCK
from nex.conversation.mail_watch_plan import PLAN_MAIL_WATCH
from nex.conversation.question_quotes import SELECT_QUOTES
from nex.conversation.reply import VERIFY_GOAL, VERIFY_WEB, WRITE, WRITE_GOAL
from nex.conversation.routing_prompt import ROUTE_HEADER
from nex.privacy.state import digest
from nex.recorded_intent import INSTRUCTIONS as RECORDED_INTENT


def tools(*calls):
    return json.dumps({"tools": list(calls)})


def model(plan, *, compose=None, code=None, language="en", web_verdict="YES", recorded=None, quotes=None,
          goal_intent="OTHER"):
    """Accept a fixed tool list or a callable of the actual turn payload."""
    def generate(messages, *_args, **_kwargs):
        prompt = messages[0]["content"]
        if prompt == CLARITY:
            return "CLEAR"
        if prompt.startswith(ROUTE_HEADER):
            return goal_intent
        if prompt == WEATHER_SCOPE:
            return "USE"
        if prompt == CHOOSE_LANGUAGE:
            return language(json.loads(messages[1]["content"])) if callable(language) else language
        if prompt == VERIFY_WEB:
            return web_verdict
        if prompt in {VERIFY_GOAL, VERIFY_GOAL_PLAN}:
            return "YES"
        if prompt == CHOOSE_GOAL_TARGET:
            return "1"
        if prompt == RECORDED_INTENT:
            if recorded is None:
                return "{}"
            return json.dumps(recorded(json.loads(messages[1]["content"])))
        if prompt == SELECT_QUOTES:
            payload = json.loads(messages[1]["content"])
            selected = quotes(payload) if quotes else [item["number"] for item in payload["candidates"][:payload["count"]]]
            return json.dumps({"indices": selected})
        if prompt in {REVIEW_GOAL_EDIT, PLAN_MAIL_WATCH, PLAN_CLOCK} or prompt.startswith((REVIEW_SCHEDULE, INSTRUCTIONS)):
            payload = json.loads(messages[1]["content"])
            if len(messages) > 2:
                payload["plan_feedback"] = messages[-1]["content"]
            calls = plan(payload) if callable(plan) else plan
            return tools(*calls)
        if prompt.startswith((WRITE_GOAL, WRITE)):
            payload = json.loads(messages[1]["content"])
            if compose:
                text = compose(payload) if callable(compose) else compose
                schedule = payload["tools"].get("goal", {}).get("schedule_text")
                return f"{text} Next run is {schedule}." if schedule and schedule not in text else text
            if python := payload["tools"].get("python"):
                if python["status"] == "ok":
                    return python["output"]
                status = python["exit_code"] if python["exit_code"] is not None else python["status"]
                return f"Python status {status}.\n\n{python['output']}".strip()
            return " ".join(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False) for value in payload["tools"].values()) or "Ready."
        if code:
            return code(messages) if callable(code) else code
        raise AssertionError("Unexpected generation prompt")
    return Mock(side_effect=generate)


def goal_clock(value, zone):
    return value.astimezone(zone).replace(tzinfo=None).isoformat(timespec="minutes")


def unlock(store, device="mac", token="open"):
    with store.privacy.transaction(write=True) as db:
        db.execute("INSERT OR REPLACE INTO sessions VALUES(?,?,?)", (digest(token), device, time() + 300))
    return token
