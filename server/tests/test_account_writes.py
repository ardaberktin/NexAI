"""Exercise connected-account write opt-ins, private routing and provider receipts."""

import asyncio
import base64
import json
import tempfile
from datetime import timedelta
from email import policy
from email.parser import BytesParser
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4

from nex.accounts.app_signin import AppSignIn
from nex.accounts.credentials import SCOPES, WRITE_SCOPES
from nex.accounts.http import AccountHTTPError
from nex.accounts.write_actions import CALENDAR_EVENTS, GMAIL_SEND, TICK_TASKS_URL
from nex.accounts.write_tools import ACTIONS
from nex.assistant import Assistant
from nex.goals import Goals
from nex.live.public_reply import public_reply
from nex.mcp import MCPError
from nex.mcp.connections import catalog
from nex.mcp.execution import execute
from nex.mcp.permissions import Access, CallApproval
from nex.privacy.public import answer
from nex.store import Store
from test_hosted_accounts import PASSWORD, HostedAccountFixture
from test_mcp_connections import NOW


def write_grant(vault, provider, settings):
    scopes = SCOPES[provider] | {WRITE_SCOPES[key] for key in settings}
    with vault.locked(provider):
        grant = vault.accept(provider, {"client_id": "nex"}, {"access_token": "fixture-token"}, requested=scopes)
        vault.write(provider, grant | {"write_settings": settings})


def tool_call(name, **arguments):
    alias, entry = next((alias, entry) for alias, entry in ACTIONS.items() if entry.tool.name == name)
    return {"name": alias, "arguments": arguments}, CallApproval(entry.tool, Access.WRITE)


class AccountProviderWrites(TestCase):
    def setUp(self):
        root = self.enterContext(tempfile.TemporaryDirectory())
        self.store = Store(root, 0, 2**20)
        self.sign_in = AppSignIn(self.store.root)
        write_grant(self.sign_in.vault, "google", {"gmail": False, "calendar": False})
        write_grant(self.sign_in.vault, "ticktick", {"ticktick": False})
        self.provider = self.enterContext(patch("nex.accounts.write_actions.request", return_value=({"id": "saved"}, {})))

    def test_gmail_sends_one_validated_mime_message(self):
        call, approval = tool_call("send_email", to="name@example.test", subject="Status", body="Done\nThanks")
        with self.assertRaises(MCPError):
            execute(self.store, call)
        self.provider.assert_not_called()
        result = execute(self.store, call, approval=approval)
        self.assertEqual(result["structured_content"], {"id": "saved", "to": "name@example.test", "subject": "Status"})
        self.assertEqual(self.provider.call_args.args, (GMAIL_SEND,))
        raw = self.provider.call_args.kwargs["payload"]["raw"]
        mail = BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(raw))
        self.assertEqual(str(mail["to"]), "name@example.test")
        self.assertEqual(mail.get_content().splitlines(), ["Done", "Thanks"])
        self.provider.reset_mock()
        for recipient in ("one@example.test, two@example.test", "one@example.test\nBcc: two@example.test", "a@"):
            result = execute(self.store, call | {"arguments": call["arguments"] | {"to": recipient}}, approval=approval)
            self.assertEqual(result["status"], "error")
        self.provider.assert_not_called()

    def test_calendar_creates_a_timed_or_all_day_event_and_reports_provider_failure(self):
        for start, end, expected in (("2026-11-25", "2026-11-26", {"date": "2026-11-25"}),
                                     ("2026-11-25T10:00-08:00", "2026-11-25T11:00-08:00",
                                      {"dateTime": "2026-11-25T10:00:00-08:00"})):
            with self.subTest(start=start):
                call, approval = tool_call("create_event", title="Test", start=start, end=end)
                self.assertEqual(execute(self.store, call, approval=approval)["status"], "ok")
                self.assertEqual(self.provider.call_args.args, (CALENDAR_EVENTS,))
                self.assertEqual(self.provider.call_args.kwargs["payload"]["start"], expected)
        self.provider.side_effect = AccountHTTPError(403)
        result = execute(self.store, call, approval=approval)
        self.assertEqual(result["status"], "error")
        self.assertNotIn("structured_content", result)

    def test_ticktick_creates_and_completes_without_guessing_a_task(self):
        create, approval = tool_call("create_task", title="Test task", due="2026-11-25T14:00-08:00", content=None, list="")
        self.assertEqual(execute(self.store, create, approval=approval)["status"], "ok")
        self.provider.assert_called_once_with(TICK_TASKS_URL, token="fixture-token", payload={
            "title": "Test task", "dueDate": "2026-11-25T14:00:00-0800",
        })
        self.provider.reset_mock()
        complete, approval = tool_call("complete_task", title="Test task")
        task = {"id": "task-1", "projectId": "my-inbox", "title": "Test task"}
        with patch("nex.accounts.ticktick.request") as lookup:
            lookup.return_value = {"tasks": [task, task | {"id": "task-2"}]}, {}
            self.assertEqual(execute(self.store, complete, approval=approval)["status"], "error")
            self.provider.assert_not_called()
            lookup.return_value = {"tasks": [task]}, {}
            self.assertEqual(execute(self.store, complete, approval=approval)["structured_content"]["id"], "task-1")
        self.provider.assert_called_once_with("https://api.ticktick.com/open/v1/project/my-inbox/task/task-1/complete",
                                              token="fixture-token", method="POST")


class AccountWriteEntryPoints(HostedAccountFixture):
    def setUp(self):
        super().setUp()
        self.store = self.host.owner.store
        self.origin = self.register_device("mac")
        self.session = self.unlock(self.origin)
        self.sign_in = self.host.owner.sign_in
        self.call, _ = tool_call("create_task", title="Buy tea")
        self.proposals = [self.call]
        self.alias = self.call["name"]
        self.provider = self.enterContext(patch("nex.accounts.write_actions.request", return_value=({"id": "task-1"}, {})))
        self.enterContext(patch("nex.accounts.ticktick.request", return_value=({"tasks": [
            {"id": "task-1", "projectId": "fixture-inbox", "title": "Buy tea"},
        ]}, {})))
        self.questions, self.outcomes = [], []
        self.review_history = []
        self.verdict = "WRITE"
        self.host.owner.assistant = Assistant(self.store, None, None)
        self.host.owner.assistant.generate_conversation = self.generate

    def generate(self, messages, *_args, **_kwargs):
        data = json.loads(messages[1]["content"])
        if candidates := data.get("mcp_candidates"):
            self.questions.append(data["latest_message"])
            names = {ACTIONS[call["name"]].tool.name for call in self.proposals}
            selected = [item["number"] for item in candidates if item["tool"] in names]
            return json.dumps({"workflow": "general", "tools": selected, "language": "en"})
        if "mcp_tools" in data:
            self.assertEqual(data["mcp_tools"][self.alias]["server"], "TickTick")
            return json.dumps({"tools": self.proposals})
        if "latest_request" in data:
            self.assertIn(data["latest_request"], self.questions)
            self.review_history = data["history"]
            return self.verdict
        if self.alias in data.get("tools", {}):
            self.outcomes.append(data["tools"][self.alias])
        self.assertNotIn("fixture-token", messages[1]["content"])
        return "Reply based on the tool outcome."

    def test_settings_require_unlock_and_remain_account_scoped(self):
        write_grant(self.sign_in.vault, "ticktick", {"ticktick": False})
        self.accounts.create("another", PASSWORD)
        guest = self.login("another")
        guest_session = self.unlock(guest)
        mode = {"account": "ticktick", "mode": "scheduled"}
        self.request("PUT", "accounts/access", body=mode, status=401)
        self.request("PUT", "accounts/access", self.origin["token"], body=mode, status=403)
        self.request("PUT", "accounts/access", guest["token"], body=mode, workspace=self.session, status=403)
        self.request("PUT", "accounts/access", guest["token"], body=mode, workspace=guest_session, status=422)
        self.assertNotIn(self.alias, catalog(self.store, scheduled=True))
        result = self.request("PUT", "accounts/access", self.origin["token"], body=mode, workspace=self.session)
        self.assertEqual(result["account_write_access"], {"ticktick": True})
        self.assertIn(self.alias, catalog(self.store, scheduled=True))
        status = self.request("GET", "status", self.origin["token"], workspace=self.session)
        self.assertEqual(status["account_write_access"], {"ticktick": True})
        self.assertNotIn("account_write_access", self.request("GET", "status", self.origin["token"]))

    def test_chat_reuses_receipts_and_voice_rechecks_permission_after_approval(self):
        write_grant(self.sign_in.vault, "ticktick", {"ticktick": False})
        text = "Put Buy tea in my TickTick inbox."
        request = {"id": str(uuid4()), "text": text, "language": "en", "time_zone": "UTC"}
        self.request("POST", "chat", self.origin["token"], body=request, status=403)
        self.provider.assert_not_called()
        for _ in range(2):
            self.request("POST", "chat", self.origin["token"], body=request, workspace=self.session)
        self.provider.assert_called_once()
        self.assertEqual(self.outcomes[-1]["structured_content"]["id"], "task-1")

        self.verdict = "DENY"
        request |= {"id": str(uuid4()), "text": "Draft a task called Buy tea for me to add."}
        self.request("POST", "chat", self.origin["token"], body=request, workspace=self.session)
        self.assertEqual(self.outcomes[-1]["status"], "denied")
        self.verdict = "WRITE"
        request |= {"id": str(uuid4()), "text": "Go ahead and add that draft to my TickTick Inbox now."}
        self.request("POST", "chat", self.origin["token"], body=request, workspace=self.session)
        self.assertIn("Draft a task called Buy tea", json.dumps(self.review_history))
        self.assertEqual(self.outcomes[-1]["status"], "ok")

        recorded_id, live_id = str(uuid4()), str(uuid4())
        result = answer(self.host.owner.assistant, self.store.privacy, recorded_id, "mac",
                        "Add a task called Buy tea.", "en", "UTC", spoken=True)
        self.assertEqual(result["engine"], "private_request")

        async def complete(_client, messages, *_tokens, **_options):
            return self.generate(messages)

        dialogue = SimpleNamespace(memory=self.store, local=None, complete=complete)
        voice = SimpleNamespace(id=live_id, device="mac", text="Create Buy tea in TickTick.", language="en", zone="UTC",
                                started=NOW, journal=None, coordinates=None, speaker="fixture")
        result = asyncio.run(public_reply(dialogue, voice, (), lambda *_: None))
        self.assertEqual(result["engine"], "private_request")
        self.sign_in.update_access({"account": "ticktick", "mode": "off"})
        for identifier in (recorded_id, live_id):
            self.request("POST", "privacy/resolve", self.origin["token"], body={"id": identifier}, workspace=self.session)
            self.assertEqual(self.outcomes[-1]["status"], "unavailable")
        self.assertEqual(self.provider.call_count, 2)

    def test_extra_write_proposed_for_same_account_is_skipped(self):
        write_grant(self.sign_in.vault, "ticktick", {"ticktick": False})
        complete, _ = tool_call("complete_task", title="Buy tea")
        self.proposals = [complete, self.call]
        request = {"id": str(uuid4()), "text": "Complete Buy tea in TickTick.", "language": "en", "time_zone": "UTC"}
        self.request("POST", "chat", self.origin["token"], body=request, workspace=self.session)
        self.provider.assert_called_once_with("https://api.ticktick.com/open/v1/project/fixture-inbox/task/task-1/complete",
                                              token="fixture-token", method="POST")
        self.assertEqual(self.outcomes[-1]["status"], "skipped")

    def test_goal_write_survives_reply_failure_without_dispatching_twice(self):
        write_grant(self.sign_in.vault, "ticktick", {"ticktick": True})
        identifier = str(uuid4())
        prompt = "Every week, create the task Buy tea in the TickTick Inbox."
        extra, _ = tool_call("complete_task", title="Buy tea")
        failed = True

        def goal_model(messages, _tokens):
            data = json.loads(messages[1]["content"])
            if "latest_request" in data:
                self.assertEqual(data["latest_request"], prompt)
                return "WRITE"
            if "evidence" in data:
                self.assertIn("task-1", data["evidence"][self.alias])
                self.assertIn("skipped", data["evidence"][extra["name"]])
                if failed:
                    raise RuntimeError("Reply interrupted")
                return "TickTick created the task."
            if "candidates" in data:
                matches = [item["number"] for item in data["candidates"]
                           if item["name"] in {"create_task", "complete_task"}]
                return json.dumps({"numbers": matches})
            if isinstance(data["mcp_tools"][self.alias], dict):
                return json.dumps({"tools": [self.call, extra]})
            return json.dumps({"sources": [self.alias]})

        goals = Goals(self.store, generate=goal_model)
        goals.save(identifier, {"prompt": prompt, "zone": "UTC", "scheduled_at": NOW.isoformat(),
                                "frequency_minutes": 10080}, NOW - timedelta(minutes=1))
        with self.assertRaisesRegex(RuntimeError, "Reply interrupted"):
            goals.run(NOW)
        failed = False
        goals.run(NOW + timedelta(minutes=1))
        self.provider.assert_called_once()
        self.assertIsNone(goals.detail(identifier)["error"])
