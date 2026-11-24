"""Disposable data and provider boundaries for the real-model assistant evaluation."""

import tempfile
from contextlib import ExitStack, contextmanager
from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

from conversation_fixtures import unlock
from nex.clock_alerts import ClockAlerts
from nex.coding.sandbox import Run
from nex.conversation.reply import run
from nex.goals import Goals
from nex.personal_files import PersonalFiles
from nex.privacy.requests import inbox, queue
from nex.store import Store
from nex.weather import Place
from nex.web_html import Page
from nex.workspace_content import artifacts

CARD_ARRIVAL = "Ask whether the new Walmart card arrived"
PUBLIC_PAGE = Page("Observatory eclipse notice", "https://example.org/eclipse",
                   "The observatory announced a solar eclipse viewing session at 10 a.m. on Monday.")


def account_fixture(_root, intent, *_args):
    if intent.provider == "gmail":
        return "Gmail", [{"subject": "Ceramics workshop", "sender": "Workshop office",
                          "body": "The pottery workshop is in Room 214 at 10 a.m."}], ""
    label = "Calendar" if intent.provider == "calendar" else "TickTick"
    title = "Garden appointment" if intent.provider == "calendar" else "Water greenhouse plants"
    return label, [{"title": title, "start": "10:00", "due": "10:00"}], ""


def seed(store, turn, group):
    moment = turn.started - timedelta(minutes=1)
    files = PersonalFiles(store.root)
    files.append("memory", "Date of birth: January 10, 1995", heading="## Synthetic facts")
    files.append("memory", "My favorite tea is jasmine.", heading="## Synthetic facts")
    with store.connect() as db:
        store.add_memory(db, "q-synthetic", moment.isoformat(), "question", "mac", "How much was the greenhouse budget?")
        store.add_memory(db, "a-synthetic", moment.isoformat(), "answer", "mac", "It was 200 dollars.", [], "local_model")
    if group == "manage_goal":
        Goals(store).save(str(uuid4()), {"title": "Plant-watering reminder", "prompt": "Remind me to water the plants.",
                                        "zone": "UTC", "scheduled_at": (turn.started + timedelta(days=5)).isoformat()},
                          anchor=turn.started)
    if group == "PDF format edit":
        Goals(store).save(str(uuid4()), {"title": "Garden report", "prompt": "Report on garden opening hours.",
                                        "zone": "UTC", "frequency_minutes": 1440,
                                        "scheduled_at": (turn.started + timedelta(days=1)).isoformat()},
                          anchor=turn.started)
    if group == "PDF removal by subject":
        goals = Goals(store)
        for title in ("Morning report", CARD_ARRIVAL, "Remind me about school assignments"):
            goal = goals.save(str(uuid4()), {"title": title, "prompt": title, "zone": "UTC",
                                             "scheduled_at": (turn.started + timedelta(days=1)).isoformat()},
                              anchor=turn.started)
            goals.set_pdf_format(goal["id"], goal["revision"], "Attach a PDF to every update.")
    if group == "clock":
        ClockAlerts(store).create(str(uuid4()), "timer", "Kitchen timer", "UTC",
                                  lambda now: now + timedelta(minutes=40))


@contextmanager
def fixture(turn, group):
    with ExitStack() as stack:
        store = Store(stack.enter_context(tempfile.TemporaryDirectory()), 0, 2**20)
        seed(store, turn, group)
        weather = {"latest_reading": {"temperature_celsius": 9}, "low_celsius": 4, "high_celsius": 9,
                   "prevailing_conditions": ["clear skies"], "hours_with_measurable_precipitation": 0}
        stack.enter_context(patch("nex.conversation.tools.Weather.locate",
                                  return_value=(Place("Victoria", "BC", "Canada", 48.43, -123.36), None)))
        stack.enter_context(patch("nex.conversation.tools.hourly", return_value=weather))
        for target in ("nex.conversation.tools.prepare_account", "nex.accounts.agenda.prepare_account",
                       "nex.goal_runner.prepare_account"):
            stack.enter_context(patch(target, side_effect=account_fixture))
        stack.enter_context(patch("nex.conversation.tools.news_pages", return_value=[PUBLIC_PAGE]))
        pages = [{"number": 1, "title": PUBLIC_PAGE.title, "url": PUBLIC_PAGE.url, "excerpt": PUBLIC_PAGE.text}]
        stack.enter_context(patch("nex.conversation.tools.prepare_lookup", return_value=(pages, ["fixture"], "")))
        # Never execute a model-authored program without the Linux sandbox.
        value = "12" if "median" in turn.text else "650"
        stack.enter_context(patch("nex.coding.answers.run_python", return_value=Run("ok", value, 0)))
        yield store


async def execute(turn, group, plan, generate):
    with fixture(turn, group) as store:
        if plan.screen:
            queue(store.privacy, turn.id, turn.device, turn.text, turn.language, turn.zone,
                  session=unlock(store), plan=plan, screen=True)
            item, = inbox(store.privacy)["items"]
            return {"passed": item["state"] == "screen" and item["answer"] is None, "engine": item["state"]}
        goals = Goals(store)
        before = goals.overview() if group in {"PDF format edit", "PDF removal by subject"} else []
        result = await run(store, turn, plan, generate, private=plan.private)
        names = {tool["name"] for tool in plan.tools}
        saved = ("create_pdf" not in names or bool(artifacts(store))) and ("goal" not in names or bool(result.get("goal_id")))
        if group == "PDF format edit":
            old, = before
            current, = goals.overview()
            saved = (result.get("goal_id") == old["id"] and bool(current.get("pdf_instruction"))
                     and all(current[key] == old[key] for key in ("prompt", "next_run", "frequency_minutes")))
        if group == "PDF removal by subject":
            target, = [item for item in before if item["title"] == CARD_ARRIVAL]
            del target["pdf_instruction"]
            target["revision"] += 1
            expected = {goal["id"]: goal for goal in before}
            saved = result.get("goal_id") == target["id"] and {goal["id"]: goal for goal in goals.overview()} == expected
        if group == "recurring PDF" and saved:
            saved = bool(goals.detail(result["goal_id"])["frequency_minutes"])
        return {"passed": bool(result["text"]) and saved, "engine": result["engine"], "text": result["text"]}


def execute_scheduled(turn, generate):
    with fixture(turn, "") as store, ExitStack() as stack:
        from nex.goal_checks import WatchCheck
        from nex.workspace_content import chat_history

        stack.enter_context(patch("nex.goal_runner.weather_evidence", return_value="Victoria: 9°C."))
        stack.enter_context(patch("nex.goal_runner.web_evidence", return_value=[
            {"title": PUBLIC_PAGE.title, "url": PUBLIC_PAGE.url, "text": PUBLIC_PAGE.text},
        ]))

        def watch(_store, _goal, _anchor, _classify, provider, _query, **_options):
            row = {"title": "Dentist", "start": "2026-11-27 10:00"}
            if provider == "gmail":
                row = {"subject": "Ceramics workshop", "sender": "Workshop office",
                       "body": "The pottery workshop is in Room 214 at 10 a.m."}
            return WatchCheck(matches=("synthetic-match",), evidence=(row,))

        stack.enter_context(patch("nex.goal_runner.check_watch", side_effect=watch))
        goals = Goals(store, generate=generate)
        goals.save(turn.id, {"prompt": turn.text, "scheduled_at": turn.started.isoformat(), "zone": turn.zone},
                   anchor=turn.started - timedelta(minutes=1), request=turn.text, language=turn.language)
        goals.run(turn.started)
        goal = goals.detail(turn.id)
        delivered = any(item.get("goal_id") == turn.id for item in chat_history(store))
        return {"passed": delivered and not goal["error"], "error": goal["error"], "text": goal["latest"],
                "pdf": any(item["kind"] == "pdf" for item in artifacts(store))}
