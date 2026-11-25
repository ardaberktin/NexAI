import base64
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock, patch

from conversation_fixtures import model
from nex.accounts.credentials import CALENDARS, EVENTS, GMAIL, TICK_TASKS, Credentials
from nex.accounts.google import calendar_events, gmail_messages
from nex.accounts.http import AccountError, AccountHTTPError
from nex.accounts.intent import AccountIntent, account_intent, date_window
from nex.accounts.read import prepare_account
from nex.accounts.ticktick import PROJECTS_URL, ticktick_items
from nex.assistant import Assistant
from nex.store import Store


class AccountIntentTests(unittest.TestCase):
    def test_only_recognizes_account_queries_and_blocks_changes(self):
        self.assertIsNone(account_intent("How does Gmail work?"))
        self.assertIsNone(account_intent("What did we discuss earlier?"))
        self.assertEqual(account_intent("Search my Gmail for solar receipt").topic, "solar receipt")
        self.assertEqual(account_intent("What is on my calendar tomorrow?").provider, "calendar")
        self.assertEqual(account_intent("List my TickTick tasks today").provider, "ticktick")
        for query in ("Send an email", "Cancel the event in my calendar", "Complete a task in TickTick"):
            self.assertTrue(account_intent(query).blocked)

    def test_date_windows_follow_local_daylight_saving(self):
        reference = datetime(2026, 11, 1, 2, tzinfo=UTC)
        start, end = date_window("What is on my calendar tomorrow?", "America/Los_Angeles", reference)
        self.assertEqual(start.isoformat(), "2026-11-01T00:00:00-07:00")
        self.assertEqual(end.isoformat(), "2026-11-02T00:00:00-08:00")

    def test_gmail_reads_scope_plain_language_filters_and_triage(self):
        for question in ("No, show me my emails about UVic", "Find emails about UVic",
                         "Search for UVic in Gmail", "Show my Gmail inbox for UVic"):
            with self.subTest(question=question):
                self.assertEqual(account_intent(question).topic, "UVic")
        overview = account_intent("look thru my emails. anything important?")
        self.assertTrue(overview.priority)
        self.assertTrue(overview.summary)
        self.assertEqual(overview.topic, "")


class CredentialsTests(unittest.TestCase):
    def test_broader_ticktick_grant_is_rejected(self):
        store = Credentials(Path("/unused"))
        with self.assertRaisesRegex(AccountError, "broader"):
            store.accept("ticktick", {"client_id": "nex"}, {
                "access_token": "test", "scope": "tasks:read tasks:write",
            })

    def test_narrow_google_grant_is_visible_without_exposing_its_secret(self):
        with tempfile.TemporaryDirectory() as root:
            vault = Credentials(root)
            client = {"client_id": "nex", "client_secret": "client-secret"}
            record = vault.accept("google", client, {"access_token": "token", "scope": GMAIL})
            with vault.locked("google"):
                vault.write("google", record)
            self.assertEqual(vault.status(), {"gmail": "connected", "calendar": "disconnected",
                                             "ticktick": "disconnected"})
            self.assertEqual((Path(root) / "accounts/google.json").stat().st_mode & 0o777, 0o600)
            with self.assertRaises(AccountError):
                vault.token("google", {EVENTS})


class ProviderTests(unittest.TestCase):
    def test_gmail_uses_bounded_alternatives_instead_of_one_phrase(self):
        vault = Mock()
        intent = AccountIntent("gmail", False, topic="UVic assignments",
                               search_variants=("UVic assignments", "Brightspace due date"))
        with patch("nex.accounts.google.get", return_value={}) as read:
            self.assertEqual(gmail_messages(vault, intent), [])
        self.assertEqual(read.call_args.args[2]["q"], '{("UVic" "assignments") ("Brightspace" "due" "date")}')

    def test_gmail_uses_metadata_reads_and_leaves_message_state_alone(self):
        vault = Mock()
        vault.token.return_value = "test"
        responses = [
            ({"messages": [{"id": "one"}]}, {}),
            ({"snippet": "It&#39;s a receipt\u200c\u200b for solar",
              "payload": {"headers": [{"name": "Subject", "value": "Solar"}]}}, {}),
        ]
        with patch("nex.accounts.google.request", side_effect=responses) as read:
            rows = gmail_messages(vault, account_intent("Show unread Gmail"))
        vault.token.assert_called_once_with("google", {GMAIL})
        self.assertEqual(rows[0]["title"], "Solar")
        self.assertEqual(rows[0]["preview"], "It's a receipt for solar")
        self.assertIn("is%3Aunread", read.call_args_list[0].args[0])
        self.assertIn("format=metadata", read.call_args_list[1].args[0])
        self.assertEqual([call.kwargs for call in read.call_args_list], [{"token": "test"}, {"token": "test"}])

        with patch("nex.accounts.google.get", return_value={}) as get:
            self.assertEqual(gmail_messages(vault, account_intent("Show me my emails about UVic")), [])
        self.assertEqual(get.call_args.args[2]["q"], '"UVic"')

    def test_scheduled_gmail_evidence_uses_read_only_full_message_content(self):
        body = "Submit the worksheet by 5 pm via Brightspace."
        sent_at = "Mon, 23 Nov 2026 09:00:00 -0800"
        responses = [
            ({"messages": [{"id": "one"}]}, {}),
            ({"internalDate": "1795449600000", "snippet": "A course update", "payload": {
                "headers": [{"name": "Subject", "value": "Worksheet"}, {"name": "Date", "value": sent_at}],
                "mimeType": "text/plain",
                "body": {"data": base64.urlsafe_b64encode(body.encode()).decode()},
            }}, {}),
        ]
        intent = AccountIntent("gmail", False, unread=True, priority=True, include_body=True)
        with (patch("nex.accounts.read.Credentials") as credentials,
              patch("nex.accounts.google.request", side_effect=responses) as read):
            vault = credentials.return_value
            vault.status.return_value = {"gmail": "connected"}
            vault.token.return_value = "test"
            _, rows, status = prepare_account(Path("/unused"), intent, "", "UTC")
        self.assertEqual(status, "")
        self.assertEqual(rows[0]["body"], body)
        self.assertEqual(rows[0]["sent_at"], sent_at)
        vault.token.assert_called_once_with("google", {GMAIL})
        self.assertIn("is%3Aunread", read.call_args_list[0].args[0])
        self.assertIn("format=full", read.call_args_list[1].args[0])
        self.assertTrue(all(call.kwargs == {"token": "test"} for call in read.call_args_list))

    def test_calendar_requests_only_list_and_event_reads(self):
        vault = Mock()
        vault.token.return_value = "test"
        response = [
            ({"items": [{"id": "calendar-id", "summary": "Work", "selected": True}]}, {}),
            ({"items": [{"summary": "Planning", "start": {"date": "2026-11-18"}}]}, {}),
        ]
        start = datetime(2026, 11, 18, tzinfo=UTC)
        end = datetime(2026, 11, 19, tzinfo=UTC)
        with patch("nex.accounts.google.request", side_effect=response) as read:
            rows = calendar_events(vault, start, end)
        vault.token.assert_called_once_with("google", {CALENDARS, EVENTS})
        self.assertEqual(rows[0]["start"], "2026-11-18 (all day)")
        self.assertTrue(all(call.kwargs == {"token": "test"} for call in read.call_args_list))

    def test_ticktick_reads_active_lists_and_inbox_with_read_only_gets(self):
        vault = Mock()
        vault.token.return_value = "test"
        responses = {
            PROJECTS_URL: [{"id": "work", "name": "Work"}, {"id": "old", "name": "Old", "closed": True}],
            PROJECTS_URL + "/inbox/data": {"tasks": [
                {"title": "Review", "dueDate": "2026-11-19T01:30:00.000+0000"},
                {"title": "Tomorrow", "dueDate": "2026-11-19T08:00:00.000+0000"},
            ]},
            PROJECTS_URL + "/work/data": {"tasks": [
                {"title": "Planning", "dueDate": "2026-11-18T00:00:00.000+0000",
                 "isAllDay": True, "timeZone": "UTC"},
                {"title": "Done", "status": 2, "dueDate": "2026-11-18T13:00:00.000+0000"},
                {"title": "Undated", "content": "Renew\n café membership"},
            ]},
        }
        query = "List my TickTick tasks today"
        reference = datetime(2026, 11, 18, 20, tzinfo=UTC)
        start, end = date_window(query, "America/Los_Angeles", reference)

        with patch("nex.accounts.ticktick.request", side_effect=lambda url, **_: (responses[url], {})) as read:
            rows = ticktick_items(vault, account_intent(query), start, end)
        self.assertEqual([(row["title"], row["due"]) for row in rows], [
            ("Planning", "2026-11-18 (all day)"), ("Review", "2026-11-18 17:30"),
        ])
        self.assertCountEqual([call.args[0] for call in read.call_args_list], responses)
        self.assertTrue(all(call.kwargs == {"token": "test"} for call in read.call_args_list))
        vault.token.assert_called_once_with("ticktick", {TICK_TASKS})

        with patch("nex.accounts.ticktick.request", side_effect=lambda url, **_: (responses[url], {})) as read:
            rows = ticktick_items(vault, account_intent("List my TickTick lists"), start, end)
            self.assertEqual(rows, [{"title": "Work"}])
            read.assert_called_once_with(PROJECTS_URL, token="test")
            rows = ticktick_items(vault, account_intent("Search TickTick for cafe membership"), start, end)
            self.assertEqual(rows, [{"title": "Undated", "priority": None}])

    def test_ticktick_chat_verifies_saved_grant_before_confirming_access(self):
        with tempfile.TemporaryDirectory() as root:
            vault = Credentials(root)
            record = vault.accept("ticktick", {"client_id": "nex"}, {"access_token": "test", "scope": TICK_TASKS})
            with vault.locked("ticktick"):
                vault.write("ticktick", record)
            assistant = Assistant(Store(root, 0, 1_048_576), None, None)
            assistant.generate_conversation = model([{"name": "ticktick", "access_check": True}])
            query = "can youa access my ticktick account?"
            for status, reply in ((200, '{"account": "TickTick", "status": "connected", "access": "read_only"}'),
                                  (401, "Reconnect TickTick"),
                                  (403, "TickTick denied read access")):
                with (self.subTest(status=status),
                      patch("nex.accounts.ticktick.request") as read):
                    if status == 200:
                        read.return_value = [], {}
                    else:
                        read.side_effect = AccountHTTPError(status)
                    result = assistant.answer(query, "en")
                    self.assertIn(reply, result["text"])
                    self.assertEqual(result["engine"], "account_read")
                    read.assert_called_once_with(PROJECTS_URL, token="test")

    def test_account_answers_never_enter_archive_or_public_search(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root, 0, 1_048_576)
            assistant = Assistant(store, None, None)
            assistant.generate_conversation = model([{"name": "gmail"}])
            with (patch("nex.conversation.tools.prepare_account", return_value=("Gmail", [{"subject": "Private receipt"}], "")),
                  patch("nex.web_answers.lookup", side_effect=AssertionError("public web read"))):
                answer = assistant.answer("Show my Gmail inbox", "en")
            self.assertIn("Private receipt", answer["text"])
            self.assertEqual(answer["engine"], "account_read")
            self.assertEqual(store.search(""), [])
