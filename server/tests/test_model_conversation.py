import asyncio
import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4
from zoneinfo import ZoneInfo

from conversation_fixtures import (
    UNSPECIFIED_PATTERN,
    action_claims,
    model,
    tools,
    unlock,
)
from nex.assistant import Assistant
from nex.conversation.calendar_scope import RESOLVE_SCOPE, repair_calendar_scopes
from nex.conversation.catalog import PLAN_CHECK_IN, REVIEW_SCHEDULE, WEATHER_SCOPE
from nex.conversation.goal_authorization import CHECK_REQUEST
from nex.conversation.goal_management_prompts import GOAL_INTENT
from nex.conversation.mail_watch_plan import CHOOSE_MAIL_ACTION, PLAN_MAIL_WATCH
from nex.conversation.plan import Plan, choose, validated
from nex.conversation.reply import REVIEW_REPLY, VERIFY_WEB, compose, run
from nex.conversation.schedule_pattern import READ_PATTERN
from nex.conversation.sync import make_turn
from nex.conversation.tools import Evidence
from nex.conversation.weather import hourly
from nex.live.public_reply import public_reply
from nex.privacy import public, requests
from nex.store import Store
from nex.weather import Place
from nex.web_html import Page

QUESTION = "How was the weather outside today?"
ZONE = "America/Vancouver"
ANCHOR = datetime.fromisoformat("2026-11-22T12:00:00-08:00")
PAST = {"name": "weather", "period": "past", "date": "2026-11-22"}
MAIL = {"name": "gmail", "query": "assignments"}


class ModelConversationTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(self.enterContext(tempfile.TemporaryDirectory()), 0, 2**20)
        self.assistant = Assistant(self.store, None, None)
        self.id = str(uuid4())
        self.turn = make_turn(self.id, "watch", QUESTION, "en", ZONE, asked_at=ANCHOR)

    def authorized_request(self):
        return json.dumps({"intent": "request", "words": self.turn.text})

    def test_same_text_can_select_different_tools_or_no_tool(self):
        for calls in ([], [PAST], [MAIL], [PAST, MAIL]):
            generate = AsyncMock(return_value=tools(*calls))
            self.assertEqual(asyncio.run(choose(self.turn, generate)), Plan(tuple(calls)))

    def test_model_can_choose_an_answer_and_language_in_one_call(self):
        self.turn.preference = "auto"
        generate = AsyncMock(return_value='{"capabilities":["answer"],"language":"en"}')
        self.assertEqual(asyncio.run(choose(self.turn, generate)), Plan(()))
        self.assertEqual(self.turn.language, "en")
        generate.assert_awaited_once()

    def test_auto_language_verifies_turkish_before_using_it_for_the_reply(self):
        self.turn.preference = "auto"
        generate = AsyncMock(side_effect=['{"capabilities":["answer"],"language":"tr"}', "en"])
        self.assertEqual(asyncio.run(choose(self.turn, generate)), Plan(()))
        self.assertEqual(self.turn.language, "en")
        self.assertEqual(generate.await_count, 2)

    def test_selected_capability_gets_its_schema_and_normalizes_a_relative_date_in_one_pass(self):
        question = "Is it likely to rain tomorrow?"
        self.turn.text = question
        generate = AsyncMock(side_effect=[
            '{"capabilities":["weather"],"language":"en"}', '{"when":"now","delivery_words":null}',
            tools({"name": "weather", "period": "forecast", "date": "tomorrow"}),
        ])
        plan = asyncio.run(choose(self.turn, generate))
        self.assertEqual(plan.tools, ({"name": "weather", "period": "forecast", "date": "2026-11-23"},))
        self.assertEqual(generate.await_count, 3)
        messages = generate.await_args.args[0]
        schema = messages.response_format["json_schema"]["schema"]
        variants = schema["properties"]["tools"]["items"]["anyOf"]
        self.assertEqual([tool["properties"]["name"]["const"] for tool in variants], ["weather"])
        self.assertEqual(json.loads(messages[1]["content"])["question"], question)

    def test_equivalent_calendar_timestamp_and_repeated_call_need_no_model_retry(self):
        args = {"name": "calendar", "start_date": "2026-11-22 00:00:00-08:00"}
        generate = AsyncMock(side_effect=[
            '{"capabilities":["calendar"],"language":"en"}', '{"when":"now","delivery_words":null}',
            tools(args, {"name": "calendar", "start_date": "2026-11-22"}),
        ])
        self.assertEqual(asyncio.run(choose(self.turn, generate)).tools,
                         ({"name": "calendar", "start_date": "2026-11-22"},))
        self.assertEqual(generate.await_count, 3)

    def test_long_pasted_python_has_an_explicit_preview_and_keeps_the_original_source(self):
        program = "# synthetic program\n" * 350 + 'print("done")'
        request = "Run Python:\n" + program
        self.turn.text = request
        data = self.turn.prompt(planning=True)
        self.assertEqual(data["pasted_program"], {"preview_only": True, "source_bytes": len(program.encode())})
        self.assertIn("Run Python:", data["question"])
        self.assertEqual(self.turn.text, request)

    def test_general_planner_uses_the_turn_clock_and_omits_specialized_examples(self):
        generate = AsyncMock(return_value=tools())
        asyncio.run(choose(self.turn, generate))
        system, payload = generate.await_args.args[0]
        self.assertEqual(json.loads(payload["content"])["local_time"],
                         ANCHOR.astimezone(ZoneInfo(ZONE)).isoformat(timespec="minutes"))
        self.assertNotIn("Set an alarm", system["content"])
        self.assertNotIn("every evening", system["content"])
        self.assertNotIn("newspaper", system["content"])

    def test_invalid_plans_retry_once_and_cannot_open_tools(self):
        for bad in ([{"name": "shell"}], [dict(MAIL, private=True)], [dict(MAIL, unread=1)],
                    [{"name": "screen"}, MAIL], [dict(PAST, query="Current")]):
            with self.subTest(bad=bad):
                generate = AsyncMock(return_value=tools(*bad))
                plan = asyncio.run(choose(self.turn, generate))
                self.assertTrue(plan.error)
                self.assertEqual(generate.await_count, 3)
                with patch("nex.conversation.reply.fetch") as fetch:
                    self.assertEqual(asyncio.run(run(self.store, self.turn, plan, generate))["engine"], "model_unavailable")
                    fetch.assert_not_called()

    def test_corrected_plan_uses_supplied_city_only(self):
        self.turn.text = "Is Victoria colder today?"
        generate = AsyncMock(side_effect=["OTHER", tools(dict(PAST, query="London")), tools(dict(PAST, query="Victoria"))])
        plan = asyncio.run(choose(self.turn, generate))
        self.assertEqual(plan.tools[0]["query"], "Victoria")

    def test_calendar_scope_is_replanned_before_an_unrecognized_date_can_widen_it(self):
        self.turn.text = "What is on my calendar next Tuesday?"
        selected = {"name": "calendar", "query": "the upcoming Tuesday", "start_date": "2026-11-24"}
        generate = AsyncMock(side_effect=["OTHER", tools({"name": "calendar", "query": "the upcoming Tuesday"}),
                                          '{"weekday":"tuesday","direction":"next"}'])
        plan = asyncio.run(choose(self.turn, generate))
        self.assertEqual(plan.tools, (selected,))
        self.assertEqual(generate.await_args.args[0][0]["content"], RESOLVE_SCOPE)
        with patch("nex.conversation.tools.prepare_account", return_value=("Calendar", [], "")) as read:
            asyncio.run(run(self.store, self.turn, plan, AsyncMock(return_value="No events."), private=True))
        self.assertEqual([day.isoformat() for day in read.call_args.args[1].date_selection],
                         ["2026-11-24", "2026-11-25"])
        generate = AsyncMock(return_value=tools({"name": "calendar", "query": "next Tuesday"}))
        plan = asyncio.run(choose(self.turn, generate))
        self.assertEqual(plan.tools[0]["start_date"], "2026-11-24")
        self.assertEqual(generate.await_count, 2)

    def test_auto_language_uses_the_models_choice_and_fixed_preferences_win(self):
        self.turn.preference = "auto"
        self.turn.text = "What happened in Türkiye today?"
        generate = AsyncMock(side_effect=["OTHER", tools(), "en", "OTHER", tools(), "tr", "OTHER", tools()])
        asyncio.run(choose(self.turn, generate))
        self.assertEqual(self.turn.language, "en")
        self.turn.text = "Bana bunu goster"
        self.turn.resolved_language = None
        asyncio.run(choose(self.turn, generate))
        self.assertEqual(self.turn.language, "tr")
        self.turn.preference = "en"
        asyncio.run(choose(self.turn, generate))
        self.assertEqual(self.turn.language, "en")

    def test_web_verifier_rejects_a_cited_claim_that_reverses_its_source(self):
        self.turn.text = "Is the observatory open on weekends?"
        page = {"number": 1, "title": "Opening hours", "excerpt": "The observatory is closed on weekends."}
        evidence = {"web": Evidence([page], ["source"], "web_lookup")}
        generate = AsyncMock(side_effect=["It’s open on weekends. [1]", "NO",
                                          "It’s closed on weekends. [1]", "YES"])
        self.assertEqual(asyncio.run(compose(self.turn, evidence, generate)), "It’s closed on weekends. [1]")
        reviews = [call.args[0] for call in generate.await_args_list if call.args[0][0]["content"] == VERIFY_WEB]
        self.assertEqual(len(reviews), 2)
        self.assertEqual(json.loads(reviews[0][1]["content"])["cited_pages"], [page])

    def test_schedule_review_can_discard_an_unsolicited_goal(self):
        self.turn.text = "My friend is going to be late."
        proposed = {"name": "goal", "query": "Check if my friend arrived", "start": "2026-11-22T13:00"}
        generate = AsyncMock(side_effect=["OTHER", tools(proposed), UNSPECIFIED_PATTERN, self.authorized_request(), tools()])
        self.assertEqual(asyncio.run(choose(self.turn, generate)), Plan(()))
        messages = generate.await_args.args[0]
        self.assertTrue(messages[0]["content"].startswith(REVIEW_SCHEDULE))
        review = json.loads(messages[1]["content"])
        self.assertEqual(review["first_choice"], "goal")
        self.assertEqual(review["question"], self.turn.text)
        self.assertEqual(review["related_tools"], [])

    def test_schedule_review_returns_an_expired_start_to_the_model(self):
        self.turn.text = "Remind me yesterday at noon to move the car."
        expired = {"name": "goal", "query": "Move the car", "timing": "yesterday at noon",
                   "start": "2026-11-21T12:00"}
        generate = AsyncMock(side_effect=["SCHEDULE", UNSPECIFIED_PATTERN, self.authorized_request(), tools(expired), tools()])
        self.assertEqual(asyncio.run(choose(self.turn, generate)), Plan(()))
        correction = generate.await_args.args[0][-1]["content"]
        self.assertIn("Ask for clarification if the requested time has passed", correction)

    def test_email_watch_retries_an_agenda_proposal_before_resolving_calendar_dates(self):
        self.turn.text = "Let me know when the committee sends its decision by email"
        self.turn.started = datetime.now(UTC)
        first_run = (self.turn.started + timedelta(hours=1)).astimezone(ZoneInfo(ZONE))
        proposed = {"name": "goal", "query": "Watch Gmail for the committee’s decision", "frequency": 60,
                    "delay_minutes": 60}
        watch = {key: value for key, value in proposed.items() if key != "delay_minutes"} | {
            "start": first_run.isoformat(timespec="minutes"), "watch_provider": "gmail", "query": self.turn.text,
        }
        generate = AsyncMock(side_effect=['{"capabilities":["mail_watch"],"language":"en"}',
                                          '{"action":"mail_watch","start":"delay"}',
                                          json.dumps(json.loads(UNSPECIFIED_PATTERN) | {"first_start": "delay"}), self.authorized_request(), tools({"name": "agenda", "query": "committee decision"}), tools(proposed)])
        with patch("nex.conversation.plan.repair_calendar_scopes", wraps=repair_calendar_scopes) as repair:
            self.assertEqual(asyncio.run(choose(self.turn, generate)), Plan((watch,)))
            self.assertEqual(repair.await_args.args[1], (watch,))
            repair.assert_awaited_once()
        prompts = [call.args[0][0]["content"] for call in generate.await_args_list]
        self.assertEqual(prompts, [GOAL_INTENT, CHOOSE_MAIL_ACTION, READ_PATTERN, CHECK_REQUEST, PLAN_MAIL_WATCH, PLAN_MAIL_WATCH])

    def test_reminder_feedback_includes_the_users_verbatim_time_phrase(self):
        self.turn.text = "Remind me tomorrow at 2 pm to send an email"
        self.turn.started = datetime.now(UTC)
        tomorrow = self.turn.started.astimezone(ZoneInfo(ZONE)).date() + timedelta(days=1)
        task = {"name": "goal", "query": "Send an email", "start": f"{tomorrow}T14:00"}
        corrected = task | {"timing": "tomorrow at 2 pm"}
        generate = AsyncMock(side_effect=["SCHEDULE", UNSPECIFIED_PATTERN, self.authorized_request(), tools(task | {"timing": "at 2 pm tomorrow"}), tools(corrected)])
        self.assertEqual(asyncio.run(choose(self.turn, generate)), Plan((corrected,)))
        self.assertIn("tomorrow at 2 pm", generate.await_args.args[0][-1]["content"])

    def test_model_checks_that_weather_applies_to_the_latest_message(self):
        self.turn.history = ({"kind": "question", "text": QUESTION}, {"kind": "answer", "text": "It was overcast."})
        self.turn.text = "The replacement pass has not arrived."
        generate = AsyncMock(side_effect=["OTHER", tools(PAST), "NO_TOOLS"])
        self.assertEqual(asyncio.run(choose(self.turn, generate)), Plan(()))
        self.assertEqual(generate.await_args.args[0][0]["content"], WEATHER_SCOPE)

    def test_writer_reconsiders_a_verbatim_reply_when_the_user_message_changes(self):
        weather = "The weather today was mostly overcast with temperatures ranging from 3 to 7 degrees Celsius."
        self.turn.history = ({"kind": "question", "text": QUESTION}, {"kind": "answer", "text": weather})
        for question, expected in (("How do analog clocks work?", "Gears move the hands at different rates."),
                                   ("Can you repeat the weather recap?", weather)):
            with self.subTest(question=question):
                self.turn.text = question
                generate = AsyncMock(side_effect=[weather, expected])
                self.assertEqual(asyncio.run(compose(self.turn, {}, generate)), expected)
                self.assertIn(REVIEW_REPLY, generate.await_args.args[0][0]["content"])

    def test_goal_writer_retries_when_the_draft_moves_the_saved_date(self):
        schedule = "November 29 at 9:00 AM"
        goal = Evidence({"prompt": "Ask whether the library card arrived", "schedule_text": schedule})
        expected = f"I’ll ask whether the card arrived on {schedule}."
        generate = AsyncMock(side_effect=["I’ll check in tomorrow at 9:00 AM.", "NO", expected, "YES"])
        self.assertEqual(asyncio.run(compose(self.turn, {"goal": goal}, generate)), expected)
        self.assertEqual(generate.await_count, 4)

    def test_goal_writer_accepts_a_verifiably_equivalent_schedule_format(self):
        goal = Evidence({"prompt": "Move the car", "schedule_text": "November 29 at 9:00 AM"})
        expected = "I saved the reminder to move the car at 9 a.m. on November 29."
        generate = AsyncMock(side_effect=[expected, "YES"])
        self.assertEqual(asyncio.run(compose(self.turn, {"goal": goal}, generate)), expected)
        self.assertEqual(generate.await_count, 2)

    def test_goal_writer_retries_an_unsupported_promise_when_the_goal_has_no_time(self):
        goal = Evidence({"prompt": "Ask whether the card arrived", "schedule_text": None, "check_in": True})
        expected = "I saved the check-in. It still needs a time."
        generate = AsyncMock(side_effect=["I’ll remind you tomorrow at 9:00 AM.", "NO", expected, "YES"])
        self.assertEqual(asyncio.run(compose(self.turn, {"goal": goal}, generate)), expected)
        self.assertEqual(generate.await_count, 4)

    def test_private_selection_on_unrecognized_wording_is_held_for_unlock(self):
        self.assistant.generate_conversation = model([MAIL], compose="Your assignment is due Friday.")
        with patch("nex.conversation.tools.prepare_account", return_value=("Gmail", [{"subject": "Friday assignment"}], "")) as read:
            result = public.answer(self.assistant, self.store.privacy, self.id, "watch", "Anything from campus?",
                                   "en", ZONE, spoken=True)
            self.assertEqual(result["engine"], "private_request")
            read.assert_not_called()
            requests.resolve(self.store.privacy, "mac", unlock(self.store), self.id, self.assistant, None)
            read.assert_called_once()
            self.assertEqual(requests.inbox(self.store.privacy)["items"][0]["answer"], "Your assignment is due Friday.")
            self.assertEqual(self.store.search("assignment"), [])
        with self.assertRaises(PermissionError):
            asyncio.run(run(self.store, self.turn, validated([MAIL]), AsyncMock(), private=False))

    def test_watch_location_and_hourly_recap_feed_the_writer_only_elapsed_hours(self):
        voice = SimpleNamespace(id=self.id, device="watch", text=QUESTION, language="en", zone=ZONE,
                                started=ANCHOR, journal=None, coordinates=None, speaker="unverified")
        dialogue = SimpleNamespace(memory=self.store, local=object())
        messages = []
        def complete(_client, prompt, _limit, **kwargs):
            messages.append(prompt)
            if len(messages) == 1:
                return "CLEAR"
            return tools(PAST) if len(messages) == 2 else "It was cool and dry, between 4 and 8 degrees."
        dialogue.complete = AsyncMock(side_effect=complete)
        from nex.live.weather_location import Coordinates
        async def locate(turn):
            turn.coordinates = Coordinates(48.43, -123.37)
        stamps = [int(ANCHOR.timestamp()) + offset * 3600 for offset in (-3, -2, 1)]
        fixture = {"hourly": {"time": stamps, "temperature_2m": [4, 8, 31], "weather_code": [0, 1, 65],
                              "precipitation": [0, 0, 10]}}
        with patch("nex.weather.read_api", return_value=fixture) as read, patch("nex.web_answers.lookup") as search:
            output = Mock()
            result = asyncio.run(public_reply(dialogue, voice, [], output, locate))
        params = read.call_args.args[1]
        self.assertEqual((params["start_date"], params["end_date"], params["latitude"]), ("2026-11-22", "2026-11-22", 48.43))
        data = json.loads(messages[-1][1]["content"])["tools"]["weather"]
        self.assertEqual((data["low_celsius"], data["high_celsius"], data["precipitation_mm"]), (4, 8, 0))
        self.assertEqual(data["hours_with_measurable_precipitation"], 0)
        self.assertEqual(data["hours_by_condition"], {"clear skies": 1, "mostly clear skies": 1})
        self.assertEqual(data["prevailing_conditions"], ["clear skies", "mostly clear skies"])
        self.assertNotIn("48.43", str(messages))
        self.assertEqual(result["engine"], "weather")
        output.assert_called_once_with(result["text"], "en")
        search.assert_not_called()

    def test_current_weather_provides_elapsed_day_and_only_fresh_current_readings(self):
        place = Place("Victoria", "BC", "Canada", 48.43, -123.37)
        stamp = int(ANCHOR.timestamp())
        rows = {"time": [stamp - 7200, stamp - 3600, stamp, stamp + 3600], "temperature_2m": [5, 6, 8, 30],
                "weather_code": [1, 1, 2, 65], "precipitation": [0, 0, 0, 20]}
        for offset in (0, 3600, -7200):
            latest = {"time": stamp + offset, "temperature_2m": 8, "apparent_temperature": 6, "weather_code": 2}
            with self.subTest(offset=offset), patch("nex.weather.read_api", return_value={"hourly": rows, "current": latest}) as read:
                report = hourly(place, ANCHOR.date(), ZONE, "current", ANCHOR)
                self.assertEqual((report["low_celsius"], report["high_celsius"], report["precipitation_mm"]), (5, 8, 0))
                self.assertEqual(report["prevailing_conditions"], ["mostly clear skies"])
                if offset == 0:
                    self.assertEqual(report["latest_reading"]["feels_like_celsius"], 6)
                else:
                    self.assertNotIn("latest_reading", report)
                read.assert_called_once()

    def test_web_title_is_never_a_spoken_fallback(self):
        self.assistant.generate_conversation = model([{"name": "web", "query": "public weather data"}],
                                                     compose="Weather Information [1]")
        with patch("nex.web_answers.lookup", return_value=[Page("Weather Information", "https://example.org", "A weather guide.")]):
            result = public.answer(self.assistant, self.store.privacy, self.id, "watch", "Find public weather data",
                                   "en", ZONE, spoken=True)
        self.assertEqual((result["engine"], result["text"]), ("web_verification_failed", ""))
        self.assertEqual(len(result["sources"]), 1)

    def test_unclear_watch_transcript_requests_clarification_without_opening_a_source(self):
        self.turn.spoken = True
        generate = AsyncMock(side_effect=["UNCLEAR", "I caught the word outside. What did you want to know?"])
        plan = asyncio.run(choose(self.turn, generate))
        with patch("nex.conversation.reply.fetch") as fetch:
            result = asyncio.run(run(self.store, self.turn, plan, generate))
        self.assertTrue(plan.clarify)
        self.assertTrue(result["text"].endswith("?"))
        fetch.assert_not_called()
        self.assertTrue(json.loads(generate.await_args.args[0][1]["content"])["clarify_transcript"])

    def test_mixed_private_plan_never_sends_generated_private_terms_to_public_search(self):
        plan = validated([MAIL, {"name": "web", "query": "unpublished tuition correspondence"}])
        self.turn.text = "Search the web for campus deadlines and check my inbox"
        generate = AsyncMock(side_effect=["I checked the inbox. I couldn’t verify public deadlines.", action_claims("checked")])
        with patch("nex.conversation.tools.prepare_account", return_value=("Gmail", [], "")), \
                patch("nex.web_answers.lookup") as lookup:
            result = asyncio.run(run(self.store, self.turn, plan, generate, private=True))
        lookup.assert_not_called()
        supplied = json.loads(generate.await_args_list[0].args[0][1]["content"])["tools"]
        self.assertIn("unavailable", supplied["web"])
        self.assertEqual(supplied["gmail"], {"status": "empty", "messages": [], "search_scope": {"query": MAIL["query"]}})
        self.assertEqual(result["sources"], [])

    def test_setting_a_weather_city_requires_an_explicit_valid_operation(self):
        for arguments in ({}, {"operation": "set"}, {"operation": "clear", "query": "Victoria"}):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                validated([{"name": "weather_city", **arguments}])
        self.assertTrue(validated([{"name":"weather_city","operation":"clear"}]).private)

    def test_check_in_route_uses_focused_schema_and_normalizes_blank_optional_fields(self):
        self.turn.text = "remind me that i'm waiting for my walmart credit card to arrive in the mail"
        self.turn.started = datetime.now(UTC)
        generate = AsyncMock(side_effect=[
            '{"capabilities":["check_in"],"language":"en"}', UNSPECIFIED_PATTERN, self.authorized_request(),
            tools({
                "name": "goal",
                "query": "Ask whether the Walmart credit card arrived",
                "check_in": True,
                "start": "",
                "timing": None,
                "frequency": None,
            }),
        ])
        plan = asyncio.run(choose(self.turn, generate))
        self.assertEqual(plan.error, "")
        self.assertEqual(plan.tools, ({
            "name": "goal",
            "query": "Ask whether the Walmart credit card arrived",
            "check_in": True,
        },))
        self.assertEqual(generate.await_count, 4)
        messages = generate.await_args.args[0]
        self.assertEqual(messages[0]["content"], PLAN_CHECK_IN)
        variants = messages.response_format["json_schema"]["schema"]["properties"]["tools"]["items"]["anyOf"]
        self.assertEqual([tool["properties"]["name"]["const"] for tool in variants], ["goal"])
        self.assertEqual(variants[0]["properties"]["check_in"], {"const": True})

    def test_goal_start_normalizes_iso_seconds_and_guides_untimed_retry(self):
        self.turn.text = "Remind me tomorrow at 2 pm to send an email"
        self.turn.started = datetime.now(UTC)
        tomorrow = self.turn.started.astimezone(ZoneInfo(ZONE)).date() + timedelta(days=1)
        generate = AsyncMock(side_effect=[
            '{"capabilities":["schedule"],"language":"en"}', UNSPECIFIED_PATTERN, self.authorized_request(),
            tools({
                "name": "goal",
                "query": "Send an email",
                "start": f"{tomorrow}T14:00:00",
                "timing": "tomorrow at 2 pm",
            }),
        ])
        plan = asyncio.run(choose(self.turn, generate))
        self.assertEqual(plan.tools[0]["start"], f"{tomorrow}T14:00")

        self.turn.text = "remind me that i'm waiting for my credit card in the mail"
        retry = AsyncMock(side_effect=[
            '{"capabilities":["check_in"],"language":"en"}', UNSPECIFIED_PATTERN, self.authorized_request(),
            tools({
                "name": "goal",
                "query": "Ask whether the credit card arrived",
                "check_in": True,
                "start": f"{tomorrow}T09:00",
                "timing": "tomorrow",
            }),
            tools({
                "name": "goal",
                "query": "Ask whether the credit card arrived",
                "check_in": True,
            }),
        ])
        recovered = asyncio.run(choose(self.turn, retry))
        self.assertEqual(recovered.error, "")
        self.assertEqual(recovered.tools, ({
            "name": "goal",
            "query": "Ask whether the credit card arrived",
            "check_in": True,
        },))
        feedback = retry.await_args.args[0][-1]["content"]
        self.assertIn("omit both start and timing", feedback)

    def test_invalid_goal_plan_asks_for_clarification_instead_of_failing_silently(self):
        self.turn.text = "remind me that i'm waiting for my walmart credit card to arrive in the mail"
        question = "I didn't save a reminder yet. Would you like me to check in later about your Walmart card, or at a specific time?"
        generate = AsyncMock(side_effect=[question, "YES"])
        with patch("nex.conversation.reply.fetch") as fetch:
            result = asyncio.run(run(self.store, self.turn, Plan((), "invalid_goal_plan"), generate, private=True))
            fetch.assert_not_called()
        self.assertEqual(result["engine"], "local_model")
        self.assertEqual(result["text"], question)

        # If verification fails or the model returns empty, fall back safely without saving a goal.
        rejected = AsyncMock(side_effect=["I saved your reminder.", "NO", "Done!", "NO"])
        with patch("nex.conversation.reply.fetch") as fetch:
            failed = asyncio.run(run(self.store, self.turn, Plan((), "invalid_goal_plan"), rejected, private=True))
            fetch.assert_not_called()
        self.assertEqual((failed["engine"], failed["text"]), ("model_unavailable", ""))

    def test_untimed_goal_writer_retry_does_not_ask_for_a_local_date_and_time(self):
        goal = Evidence({"prompt": "Ask whether the card arrived", "schedule_text": None, "check_in": True})
        expected = "I saved a check-in about whether your card arrived."
        generate = AsyncMock(side_effect=["I’ll remind you tomorrow at 9:00 AM.", "NO", expected, "YES"])
        self.assertEqual(asyncio.run(compose(self.turn, {"goal": goal}, generate)), expected)
        retry_system = generate.await_args_list[2].args[0][0]["content"]
        self.assertIn("schedule_text is null", retry_system)
        self.assertNotIn("including its local date and time", retry_system)
