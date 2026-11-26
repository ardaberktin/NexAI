"""Exercise account privacy settings and typed Chat through authenticated HTTPS."""

from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

from conversation_fixtures import model
from nex.conversation.tools import Evidence
from nex.privacy.chat_settings import DEFAULTS
from nex.web_html import Page
from test_hosted_accounts import PASSWORD, HostedAccountFixture


class ChatPrivacySettingsTests(HostedAccountFixture):
    def setUp(self):
        super().setUp()
        self.phone = self.register_device("iphone")
        self.session = self.unlock(self.phone)
        self.store = self.host.owner.store
        self.assistant = Mock(store=self.store, settings=None)
        self.host.owner.assistant = self.assistant

    def call(self, method, path, body=None, *, status=200):
        return self.request(method, path, self.phone["token"], workspace=self.session, body=body, status=status)

    def setting(self, name, value):
        return self.call("PUT", "workspace/chat-privacy", {"setting": name, "enabled": value})

    def ask(self, text, *, identifier=None, path="chat", status=200, **options):
        return self.call("POST", path, {"id": identifier or str(uuid4()), "text": text, "language": "en", **options},
                         status=status)

    def test_settings_require_unlock_and_sync_only_within_the_account(self):
        path = "workspace/chat-privacy"
        change = {"setting": "general", "enabled": True}
        self.request("PUT", path, body=change, status=401)
        self.request("PUT", path, self.phone["token"], body=change, status=403)
        self.request("GET", path, self.phone["token"], status=403)
        self.assertEqual(self.call("GET", path), DEFAULTS)
        mac = self.register_device("mac", self.user)
        mac_session = self.unlock(mac, self.user)
        expected = DEFAULTS | {"general": True}
        self.assertEqual(self.setting("general", True), expected)
        self.assertEqual(self.request("GET", path, mac["token"], workspace=mac_session), expected)
        self.assertEqual(self.call("GET", "status")["chat_privacy"], expected)
        self.assertNotIn("chat_privacy", self.request("GET", "status", mac["token"]))
        self.accounts.create("guest", PASSWORD)
        guest = self.login("guest")
        self.assertEqual(self.request("GET", path, guest["token"], workspace=self.unlock(guest)), DEFAULTS)
        for invalid in ({"setting": "voice", "enabled": True}, {"setting": "web", "enabled": 1}):
            self.call("PUT", path, invalid, status=400)
        self.assertEqual(self.call("GET", path), expected)

    def test_defaults_and_overrides_keep_private_text_out_of_shared_history(self):
        observed = []

        def compose(data):
            observed.append(data)
            return "Evidence-based answer to " + data["question"]

        self.assistant.generate_conversation = model([], compose=compose)
        self.assertEqual(self.ask("Explain punctuation.")["engine"], "local_model")
        self.setting("general", True)
        for question in ("Why does the tide rise?", "What makes ocean levels change during the day?"):
            self.assertEqual(self.ask(question)["engine"], "private_request")
        self.assertEqual(self.ask("Explain alliteration.", access="standard")["engine"], "local_model")
        self.assertEqual(observed[-1]["history"], [])
        self.setting("enabled", False)
        self.assertEqual(self.ask("What does an eclipse look like?")["engine"], "local_model")
        self.ask("Describe a prism.", access="private", status=409)
        self.assertEqual(self.ask("A quoted private answer", access="private", private_reply=True)["engine"],
                         "private_request")
        self.setting("enabled", True)
        self.assertEqual(self.ask("Why do leaves change color?")["engine"], "private_request")
        private = self.call("GET", "privacy/inbox")["items"]
        shared = self.call("GET", "workspace/chat?paged=1")["items"]
        self.assertEqual({item["question"] for item in private}, {
            "Why does the tide rise?", "What makes ocean levels change during the day?",
            "A quoted private answer", "Why do leaves change color?",
        })
        self.assertEqual({item["text"] for item in shared if item["role"] == "user"}, {
            "Explain punctuation.", "Explain alliteration.", "What does an eclipse look like?",
        })
        self.assertTrue(all(item["answer"] == "Evidence-based answer to " + item["question"] for item in private))

    def test_standard_cannot_publish_required_private_tool_results(self):
        self.assistant.generate_conversation = model([{"name": "gmail"}], compose="Protected account fixture")
        self.setting("enabled", False)
        with patch("nex.conversation.tools.prepare_account", return_value=("Gmail", [], "")):
            self.assertEqual(self.ask("Read my inbox.", access="standard")["engine"], "private_request")
        self.assertEqual(self.call("GET", "workspace/chat?paged=1")["items"], [])
        self.assertEqual(self.call("GET", "privacy/inbox")["items"][0]["answer"], "Protected account fixture")

    def test_private_web_uses_verified_links_without_archiving_pages(self):
        topic = "Aurora Museum opening"
        text = "Search the web for " + topic
        page = Page(topic, "https://example.com/museum", "The Aurora Museum opening is on Saturday.")
        self.assistant.generate_conversation = model([{"name": "web", "query": topic}],
                                                    compose="The Aurora Museum opens on Saturday [1].")
        self.setting("web", True)
        with patch("nex.web_answers.lookup", return_value=[page]) as lookup:
            self.assertIsNone(self.ask(text, path="web/chat"))
            lookup.assert_not_called()
            self.assertEqual(self.ask(text)["engine"], "private_request")
            lookup.assert_called_once()
        self.assertEqual(self.call("GET", "workspace/chat?paged=1")["items"], [])
        self.assertIn("[1](<https://example.com/museum>)", self.call("GET", "privacy/inbox")["items"][0]["answer"])
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM memory").fetchone()[0], 0)

    def test_calculation_default_uses_the_model_plan(self):
        self.setting("python", True)
        self.assistant.generate_conversation = model([{"name": "python"}], compose="Twenty-one.")
        result = Evidence({"status": "ok", "output": "21"}, engine="python_sandbox")
        compute = AsyncMock(return_value=result)
        with patch("nex.conversation.reply.compute", compute):
            self.assertEqual(self.ask("Divide 63 by 3.")["engine"], "private_request")
            self.assertEqual(self.ask("What is a third of 63?", access="standard")["engine"], "python_sandbox")
        self.assertEqual([call.args[0].text for call in compute.await_args_list],
                         ["Divide 63 by 3.", "What is a third of 63?"])
        self.assertEqual(self.call("GET", "privacy/inbox")["items"][0]["answer"], "Twenty-one.")

    def test_retries_preserve_storage_after_settings_change_and_dismissal(self):
        self.setting("weather", True)
        self.assistant.generate_conversation = model([{"name": "weather", "period": "current"}])
        identifier = str(uuid4())
        text = "How's the weather?"
        options = {"identifier": identifier, "weather_location_supported": True}
        self.assertEqual(self.ask(text, **options)["engine"], "weather_location_request")
        self.setting("weather", False)
        response = self.ask(text, **options, weather_location={"id": identifier})
        self.assertEqual(response["engine"], "private_request")
        self.call("POST", "privacy/dismiss", {"id": identifier})
        self.ask(text, **options, access="standard", status=400)
        self.assertEqual(self.call("GET", "workspace/chat?paged=1")["items"], [])

        identifier = str(uuid4())
        options["identifier"] = identifier
        self.assertEqual(self.ask(text, **options)["engine"], "weather_location_request")
        self.setting("weather", True)
        response = self.ask(text, **options, weather_location={"id": identifier})
        self.assertEqual(response["engine"], "weather_city_needed")
        self.assertEqual(self.call("GET", "privacy/inbox")["items"], [])
