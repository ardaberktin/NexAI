"""Script the model boundary while exercising the production planner, tools and storage."""
import json
from time import time
from unittest.mock import Mock

from nex.conversation.catalog import (
    CHOOSE_GOAL_TARGET,
    CHOOSE_LANGUAGE,
    CLARITY,
    INSTRUCTIONS,
    PLAN_CHECK_IN,
    REVIEW_GOAL_EDIT,
    REVIEW_SCHEDULE,
    VERIFY_GOAL_PLAN,
    WEATHER_SCOPE,
)
from nex.conversation.clock_tool import PLAN_CLOCK
from nex.conversation.goal_authorization import CHECK_REQUEST
from nex.conversation.goal_edit_receipt import VERIFY_WATCH, WRITE_WATCH
from nex.conversation.mail_watch_plan import (
    CHOOSE_MAIL_ACTION,
    MAIL_WATCH_ROUTES,
    PLAN_MAIL_CHECK_AND_WATCH,
    PLAN_MAIL_WATCH,
)
from nex.conversation.question_quotes import SELECT_QUOTES
from nex.conversation.reply import (
    VERIFY_GOAL,
    VERIFY_WEB,
    WRITE,
    WRITE_GOAL,
    WRITE_MAIL_WATCH,
)
from nex.conversation.routing_prompt import ROUTE_HEADER
from nex.conversation.schedule_pattern import READ_PATTERN, REVIEW_READ_TIME
from nex.conversation.verify_tool_reply import VERIFY_TOOL_REPLY
from nex.privacy.state import digest
from nex.privacy.voice import WRITE_APPROVAL_NOTICE
from nex.recorded_intent import INSTRUCTIONS as RECORDED_INTENT


def tools(*calls):
    return json.dumps({"tools": list(calls)})


def action_claims(mail_search="none", *, saved_watch=False, cadence=None, next_run_matches=True, instant_detection=False):
    return json.dumps({"mail_search": mail_search, "saved_watch": saved_watch, "cadence": cadence,
                       "wrong_next_check": not next_run_matches, "unsupported_delivery": instant_detection})


UNSPECIFIED_PATTERN = json.dumps({"pattern": "unspecified", "count": None, "unit": None, "weekdays": [], "clock": None, "first_start": "implicit"})


def model(plan, *, compose=None, code=None, language="en", web_verdict="YES", recorded=None, quotes=None,
          goal_intent="OTHER", approval_notice=None, mail_action=None, watch_start="cadence"):
    """Accept a fixed tool list or a callable of the actual turn payload."""
    current_tools = {}

    def generate(messages, *_args, **_kwargs):
        nonlocal current_tools
        prompt = messages[0]["content"]
        if prompt == CLARITY:
            return "CLEAR"
        if prompt.startswith(ROUTE_HEADER):
            return goal_intent
        if prompt == REVIEW_READ_TIME:
            return '{"when":"now","delivery_words":null}'
        if prompt == CHECK_REQUEST:
            return json.dumps({"intent": "request", "words": json.loads(messages[1]["content"])["latest_message"]})
        if prompt == READ_PATTERN:
            return json.dumps(json.loads(UNSPECIFIED_PATTERN) | {"first_start": "implicit" if watch_start == "cadence" else watch_start})
        if prompt == CHOOSE_MAIL_ACTION:
            capabilities = json.loads(goal_intent).get("capabilities", []) if goal_intent.startswith("{") else []
            action = mail_action or next((value for value in capabilities if value in MAIL_WATCH_ROUTES), "other")
            return json.dumps({"action": action, "start": watch_start})
        if prompt == WEATHER_SCOPE:
            return "USE"
        if prompt == CHOOSE_LANGUAGE:
            return language(json.loads(messages[1]["content"])) if callable(language) else language
        if prompt == WRITE_APPROVAL_NOTICE:
            text = approval_notice(json.loads(messages[1]["content"])) if callable(approval_notice) else approval_notice
            return json.dumps({"notice": text})
        if prompt == VERIFY_WEB:
            return web_verdict
        if prompt in {VERIFY_GOAL, VERIFY_GOAL_PLAN, VERIFY_WATCH}:
            return "YES"
        if prompt == VERIFY_TOOL_REPLY:
            gmail, goal = current_tools.get("gmail"), current_tools.get("goal", {})
            mail_search = "none" if gmail is None else "unavailable" if gmail["status"] == "unavailable" else "checked"
            return action_claims(mail_search, saved_watch="schedule_text" in goal or goal.get("saved") is True, cadence=goal.get("frequency_minutes"))
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
        if prompt in {REVIEW_GOAL_EDIT, PLAN_MAIL_WATCH, PLAN_MAIL_CHECK_AND_WATCH, PLAN_CLOCK, PLAN_CHECK_IN} or (
                prompt.startswith((REVIEW_SCHEDULE, INSTRUCTIONS, "Fill the arguments for the selected capabilities"))):
            payload = json.loads(messages[1]["content"])
            if len(messages) > 2:
                payload["plan_feedback"] = messages[-1]["content"]
            calls = plan(payload) if callable(plan) else plan
            return tools(*calls)
        if prompt.startswith((WRITE_GOAL, WRITE_WATCH, WRITE_MAIL_WATCH, WRITE)):
            payload = json.loads(messages[1]["content"])
            current_tools = payload["tools"]
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
