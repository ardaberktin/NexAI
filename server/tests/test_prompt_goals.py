import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock, patch
from uuid import uuid4

from nex.feed import stamp
from nex.goal_runner import MODEL_ERROR
from nex.goals import Goals
from nex.store import Store
from nex.workspace_content import artifacts, chat_history


class PromptGoalTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = Store(directory.name, 0, 2**20)
        self.generate = Mock()
        self.goals = Goals(self.store, generate=self.generate)
        self.id = str(uuid4())
        self.now = datetime(2026, 10, 31, 14, tzinfo=UTC)
        self.due = self.now + timedelta(hours=1)

    def save(self, prompt, frequency=0, **changes):
        data = {"prompt": prompt, "frequency_minutes": frequency, "scheduled_at": self.due.isoformat(),
                "zone": "America/Los_Angeles"} | changes
        return self.goals.save(self.id, data, anchor=self.now)

    def test_local_model_combines_tools_and_publishes_a_daily_update_at_the_local_time(self):
        prompt = "Give me today's meetings and current weather in a short briefing."
        self.save(prompt, 1440)
        self.generate.side_effect = [
            '{"tools":[{"name":"calendar","query":"today"},{"name":"weather","query":"Victoria"}]}',
            "Lab at 10. It’s 9°C in Victoria.",
        ]
        with self.store.connect() as db:
            db.execute("INSERT INTO iphone_push_registration VALUES(1,?,?,?)",
                       (str(uuid4()), "ab" * 32, "development"))
        with (patch("nex.goal_runner.prepare_account", return_value=("Calendar", [{"title": "Lab"}], "")) as calendar,
              patch("nex.goal_runner.weather_evidence", return_value="9°C in Victoria") as weather):
            self.goals.run(self.due - timedelta(seconds=1))
            self.generate.assert_not_called()
            self.goals.run(self.due)
            self.goals.run(self.due)
        self.assertEqual(calendar.call_args.args[1].provider, "calendar")
        self.assertEqual(weather.call_args.args[-1], "Victoria")
        model_input = json.loads(self.generate.call_args.args[0][1]["content"])
        self.assertEqual(model_input["goal"], prompt)
        self.assertIn("Lab", model_input["evidence"]["calendar"])
        self.assertIn("9°C", model_input["evidence"]["weather"])
        update, = chat_history(self.store)
        self.assertEqual((update["goal_id"], update["text"]), (self.id, "Lab at 10. It’s 9°C in Victoria."))
        self.assertEqual(artifacts(self.store, self.id)[0]["text"], update["text"])
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT id FROM goal_push_delivery").fetchone()[0], update["id"])
        self.assertEqual(self.goals.detail(self.id)["next_run"], stamp(datetime(2026, 11, 1, 16, tzinfo=UTC)))

    def test_goal_runs_receive_original_request_saved_language_and_current_definition(self):
        original = "Her gün bitkiler hakkında Türkçe bir özet gönder."
        draft = {"prompt": "Summarize the plants every day.", "frequency_minutes": 1440,
                 "scheduled_at": stamp(self.due), "zone": "America/Los_Angeles"}
        saved = self.goals.save(self.id, draft, anchor=self.now, request=original, language="tr")
        amended = "Summarize the garden lights every day."
        self.goals.save(self.id, draft | {"prompt": amended, "revision": saved["revision"]}, anchor=self.now)
        self.generate.side_effect = ['{"tools":[]}', "Bahçe ışıkları açık."]
        self.goals.run(self.due)
        for call in self.generate.call_args_list:
            data = json.loads(call.args[0][1]["content"])
            self.assertEqual((data["original_request"], data["language"], data["goal"], data["time_zone"]),
                             (original, "tr", amended, draft["zone"]))
        self.assertEqual(chat_history(self.store)[0]["text"], "Bahçe ışıkları açık.")

    def test_scheduled_source_selection_scopes_arguments_to_the_selected_provider(self):
        self.save("List tomorrow's Google Calendar appointments.")
        self.generate.side_effect = [
            '{"sources":["calendar"]}', '{"tools":[{"name":"calendar","query":"tomorrow"}]}', "Lab at 10.",
        ]
        with patch("nex.goal_runner.prepare_account", return_value=("Calendar", [{"title": "Lab"}], "")) as calendar:
            self.goals.run(self.due)
        calendar.assert_called_once()
        self.assertEqual(calendar.call_args.args[1].provider, "calendar")
        planned = self.generate.call_args_list[1].args[0]
        self.assertIn("calendar: Named Google Calendar", planned[0]["content"])
        self.assertNotIn("gmail:", planned[0]["content"])
        self.assertEqual(chat_history(self.store)[0]["text"], "Lab at 10.")

    def test_goal_context_migration_recovers_saved_language(self):
        draft = {"prompt": "A legacy goal", "scheduled_at": stamp(self.due), "zone": "UTC"}
        self.goals.save(self.id, draft, anchor=self.now, request="Orijinal görev", language="tr")
        with self.store.connect() as db:
            db.execute("ALTER TABLE goals DROP COLUMN original_request")
            db.execute("ALTER TABLE goals DROP COLUMN reply_language")
        upgraded = Store(self.store.root, 0, 2**20)
        with upgraded.connect() as db:
            row = db.execute("SELECT original_request,reply_language FROM goals WHERE id=?", (self.id,)).fetchone()
        self.assertEqual(tuple(row), ("Orijinal görev", "tr"))

    def test_run_now_preserves_a_future_one_time_run(self):
        goal = self.save("Remind me to stretch")
        self.generate.side_effect = ['{"tools":[]}', "Time to stretch."] * 2
        self.goals.action(self.id, "check", goal["revision"], anchor=self.now)
        self.goals.run(self.now)
        self.assertEqual(self.goals.detail(self.id)["next_run"], stamp(self.due))
        self.goals.run(self.due)
        self.goals.run(self.due + timedelta(minutes=1))
        self.assertIsNone(self.goals.detail(self.id)["next_run"])
        self.assertEqual(len(chat_history(self.store)), 2)

    def test_invalid_plan_retries_and_a_conditional_goal_can_return_no_update(self):
        self.save("Alert me if there are any meetings tomorrow", 60)
        self.generate.side_effect = [
            '{"tools":[{"name":"send_email"}]}', '{"tools":[{"name":"calendar","query":"tomorrow"}]}', "NO_UPDATE",
        ]
        self.goals.run(self.due)
        self.assertEqual(self.goals.detail(self.id)["error"], MODEL_ERROR)
        with patch("nex.goal_runner.prepare_account", return_value=("Calendar", [], "I found no matching items.")):
            self.goals.run(self.due + timedelta(minutes=5))
        goal = self.goals.detail(self.id)
        self.assertIsNone(goal["error"])
        self.assertEqual(goal["next_run"], stamp(self.due + timedelta(hours=1)))
        self.assertEqual(chat_history(self.store), [])

    def test_model_selected_gmail_trigger_does_not_repeat_the_same_message(self):
        self.save("Notify me about inbox messages concerning lab deadlines", 15)
        plan = '{"tools":[{"name":"new_mail"}]}'
        self.generate.side_effect = [plan, "The lab report is due Friday.\nNO_UPDATE", plan]
        self.goals.classify = Mock(return_value="YES")
        received = int(self.due.timestamp() * 1000)
        body = "Here is an update about the class. " * 8 + "The deadline is this Friday."
        with (patch("nex.goal_checks.Credentials") as credential,
              patch("nex.goal_checks.gmail_watch.message_ids", return_value=["mail-id"]),
              patch("nex.goal_checks.gmail_watch.message", return_value=(
                  received, {"subject": "Lab report deadlines", "body": body},
              )) as read):
            credential.return_value.status.return_value = {"gmail": "connected"}
            self.goals.run(self.due)
            self.goals.run(self.due + timedelta(minutes=15))
        read.assert_called_once()
        self.goals.classify.assert_called_once()
        messages, token_budget = self.generate.call_args_list[1].args
        self.assertIn("one specific thing prepared in Chat", messages[0]["content"])
        self.assertGreater(token_budget, 425)
        delivery_prompt = json.loads(messages[1]["content"])
        self.assertIn("The deadline is this Friday.", delivery_prompt["evidence"]["new_mail"][0]["excerpt"])
        self.assertEqual([update["text"] for update in chat_history(self.store)], ["The lab report is due Friday."])

    def test_prompt_briefing_passes_unread_email_bodies_to_the_writer(self):
        self.save("Every morning give me my agenda, the weather, and anything important", 1440)
        self.generate.side_effect = [
            '{"tools":[{"name":"agenda","query":"today"},{"name":"weather"},{"name":"gmail","unread":true}]}',
            "The lab moved to room 214. Want me to draft a note to your lab partner with the new room?",
        ]
        mail = [{"subject": "A location change", "sender": "Course team", "body": "The lab moved to room 214."}]
        with (patch("nex.goal_runner.prepare_account", return_value=("Gmail", mail, "")) as gmail,
              patch("nex.goal_runner.agenda_evidence", return_value={"items": [{"title": "Lab", "time": "10:00"}]}),
              patch("nex.goal_runner.weather_evidence", return_value="9°C in Victoria")):
            self.goals.run(self.due)
        intent = gmail.call_args.args[1]
        self.assertTrue(intent.unread and intent.include_body)
        model_input = json.loads(self.generate.call_args.args[0][1]["content"])
        self.assertEqual(model_input["evidence"]["gmail"][0]["excerpt"], mail[0]["body"])
        self.assertIn("draft a note", chat_history(self.store)[0]["text"])

    def test_ai_can_combine_new_calendar_events_with_other_sources(self):
        self.save("Tell me about new dental appointments and include the weather", 15)
        plan = '{"tools":[{"name":"new_calendar","query":"dental appointment"},{"name":"weather"}]}'
        self.generate.side_effect = [plan, "Dentist next week. It’s 9°C in Victoria.", plan]
        classify = Mock(return_value="YES")
        self.goals.classify = classify
        event = {"title": "Dentist", "start": "2026-11-04 10:00", "end": "2026-11-04 11:00",
                 "calendar": "School", "location": "Campus"}
        with (patch("nex.goal_checks.Credentials") as credentials,
              patch("nex.goal_checks.calendar_events", return_value=[event]),
              patch("nex.goal_runner.weather_evidence", return_value="9°C in Victoria") as weather):
            credentials.return_value.status.return_value = {"calendar": "connected"}
            self.goals.run(self.due)
            self.goals.run(self.due + timedelta(minutes=15))
        weather.assert_called_once()
        update, = chat_history(self.store)
        self.assertIn("Dentist", update["text"])
        classify.assert_called_once()

    def test_pausing_while_the_model_is_working_discards_the_result(self):
        goal = self.save("Give me a writing exercise")

        def pause(_, tokens):
            if tokens == 224:
                return '{"tools":[]}'
            self.goals.action(self.id, "paused", goal["revision"], anchor=self.due)
            return "Describe a sound you heard this morning."

        self.generate.side_effect = pause
        self.goals.run(self.due)
        self.assertEqual(chat_history(self.store), [])
        self.assertEqual(self.goals.detail(self.id)["status"], "paused")

    def test_deleting_a_running_goal_discards_its_result_and_skips_other_deleted_work(self):
        goal = self.save("Give me a writing exercise")
        later = self.due + timedelta(minutes=1)
        other = self.goals.save(str(uuid4()), {
            "prompt": "Give me a sketch idea", "scheduled_at": stamp(later), "zone": "UTC",
        }, anchor=self.now)

        def delete(_, tokens):
            if tokens == 224:
                return '{"tools":[]}'
            self.goals.delete(self.id, goal["revision"])
            self.goals.delete(other["id"], other["revision"])
            return "Describe a sound you heard this morning."

        self.generate.side_effect = delete
        self.goals.run(later)
        self.assertEqual(self.generate.call_count, 2)
        self.assertEqual(chat_history(self.store), [])
        self.assertEqual(self.goals.overview(), [])

    def test_type_only_payload_cannot_create_a_goal(self):
        with self.assertRaisesRegex(ValueError, "Describe what you want Nex to do"):
            self.goals.save(self.id, {"kind": "reminder", "title": "Take out the recycling"}, anchor=self.now)
        self.assertEqual(self.goals.overview(), [])
