import json
import unittest
from datetime import UTC, datetime, timedelta
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
from uuid import uuid4

from conversation_fixtures import action_claims, unlock
from nex.conversation.reply import WRITE, WRITE_MATCHED
from nex.conversation.routing_prompt import OFFER_RECALL
from nex.conversation.sync import make_turn
from nex.conversation.verify_tool_reply import VERIFY_TOOL_REPLY
from nex.conversation_history import time_key
from nex.privacy import public
from nex.privacy.chat_history import ChatFollowups
from nex.privacy.chat_recall import RECENT_LIMIT, recall_chat
from nex.privacy.requests import inbox
from nex.privacy.state import AccessDenied
from nex.store import Store


class ChatHistoryRecallTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(TemporaryDirectory())
        self.store = Store(directory, 0, 2**20)
        self.session = unlock(self.store)
        self.followups = ChatFollowups()
        self.assistant = Mock(store=self.store, settings=None)
        self.started = datetime.now(UTC) - timedelta(days=2)

    def add_exchange(self, question, answer, *, elapsed=0, engine="local_model", identifier=None):
        identifier = identifier or str(uuid4())
        stamp = time_key(self.started + timedelta(minutes=elapsed))
        with self.store.connect() as db:
            self.store.add_memory(db, "q-" + identifier, stamp, "question", "iphone", question)
            if answer is not None:
                self.store.add_memory(db, "a-" + identifier, stamp, "answer", "iphone", answer, engine=engine)
        return identifier

    def ask(self, text, *, identifier=None, **options):
        defaults = {"session": self.session, "followups": self.followups}
        return public.chat(self.assistant, identifier or str(uuid4()), "mac", text, "en", "UTC", **(defaults | options))

    def script(self, query, compose, *, route_after="answer", tools=(), match=None):
        observed = []

        def generate(messages, *_args, **_kwargs):
            instructions = messages[0]["content"]
            data = json.loads(messages[1]["content"])
            if instructions.startswith("Select capabilities"):
                observed.append(data)
                if "recalled_chat" in data:
                    self.assertNotIn(OFFER_RECALL, instructions)
                    route = {"capabilities": [route_after], "language": "en"}
                    if match:
                        route["exchange"] = match(data["recalled_chat"]["exchanges"])
                    return json.dumps(route)
                if OFFER_RECALL in instructions:
                    return json.dumps({"capabilities": ["recall_chat"], "language": "en", "query": query})
                return '{"capabilities":["answer"],"language":"en"}'
            if instructions == VERIFY_TOOL_REPLY:
                return action_claims()
            if instructions.startswith((WRITE, WRITE_MATCHED)):
                return compose(data)
            self.assertTrue(tools, instructions)
            observed.append(data)
            return json.dumps({"tools": tools})

        self.assistant.generate_conversation.side_effect = generate
        return observed

    def test_an_implicit_reference_can_recall_an_older_exchange_from_the_other_device(self):
        self.add_exchange("Can you draft a checklist for launch?", "App Store release: verify the screenshots and pricing.")
        for index in range(1, 9):
            identifier = self.add_exchange(f"Later question {index}", f"Later answer {index}", elapsed=index)
            self.followups.remember("mac", self.session, identifier, f"Later question {index}",
                                    {"text": f"Later answer {index}", "engine": "local_model"})
        hidden = "App Store release: an unfinished draft"
        self.add_exchange(hidden, None, elapsed=9)
        self.add_exchange(hidden, "Private handoff", elapsed=10, engine="private_request")
        expected = [{"role": "user", "text": "Can you draft a checklist for launch?"},
                    {"role": "assistant", "text": "App Store release: verify the screenshots and pricing."}]

        for request in ("Could you trim that App Store checklist we made?",
                        "Make the release checklist from our earlier chat into one line."):
            with self.subTest(request=request):
                def compose(data, request=request):
                    self.assertEqual(data["question"], request)
                    self.assertEqual(data["previous_question"], expected[0]["text"])
                    self.assertEqual(data["previous_answer"], expected[1]["text"])
                    return "Verify App Store screenshots and pricing."

                routes = self.script("App Store checklist", compose)
                self.assertEqual(self.ask(request)["text"], "Verify App Store screenshots and pricing.")
                initial, reroute = routes
                self.assertEqual(initial["latest_message"], request)
                self.assertNotIn(expected[-1]["text"], json.dumps(initial))
                self.assertIn(expected[-1]["text"], json.dumps(reroute))

    def test_vague_reference_can_browse_a_bounded_window_excluding_current_and_cached_exchanges(self):
        anchor = "The checklist for the community open house"
        self.add_exchange(anchor, "Confirm the room and prepare name tags.")
        for index in range(10):
            self.add_exchange(f"Unrelated subject {index}", "Unrelated answer", elapsed=index - 8)
        duplicate = self.add_exchange("Already in recent history", "A recent reply", elapsed=15)
        self.followups.remember("mac", self.session, duplicate, "Already in recent history",
                                {"text": "A recent reply", "engine": "local_model"})
        def compose(data):
            exchanges = data["recalled_chat"]["exchanges"]
            self.assertEqual(len(exchanges), RECENT_LIMIT)
            self.assertIn(anchor, json.dumps(exchanges))
            self.assertNotIn("Already in recent history", json.dumps(exchanges))
            return "Confirm the room and prepare name tags."

        self.script("", compose)
        self.assertEqual(self.ask("Can we go back to the one from earlier?")["text"],
                         "Confirm the room and prepare name tags.")
        identifier = self.add_exchange("Current exchange", "An old answer", elapsed=16)
        turn = make_turn(identifier, "mac", "Follow up with the current exchange", "en", "UTC")
        self.assertNotIn("Current exchange", json.dumps(recall_chat(self.store, turn, self.session, "")))

    def test_model_can_select_one_matching_exchange_from_multiple_candidates(self):
        self.add_exchange("Floor plan for the bike shed", "Put the racks on the east wall.")
        self.add_exchange("Floor plan for the shop", "Put the till by the exit.", elapsed=1)

        def match(exchanges):
            self.assertEqual(len(exchanges), 2)
            return next(index for index, exchange in enumerate(exchanges, start=1)
                        if "bike shed" in json.dumps(exchange))

        def compose(data):
            self.assertIn("east wall", data["previous_answer"])
            self.assertNotIn("by the exit", json.dumps(data))
            return "On the east wall."

        self.script("floor plan", compose, match=match)
        self.assertEqual(self.ask("Which wall gets the bike racks from the floor plan we made?")["text"],
                         "On the east wall.")

    def test_regular_requests_and_chat_without_a_session_skip_the_lookup(self):
        def answer(messages, *_args, **_kwargs):
            data = json.loads(messages[1]["content"])
            self.assertNotIn("recalled_chat", data)
            if messages[0]["content"].startswith("Select capabilities"):
                return '{"capabilities":["answer"],"language":"en"}'
            return "A direct answer."

        self.assistant.generate_conversation.side_effect = answer
        with patch("nex.privacy.public.recall_chat") as lookup:
            self.assertEqual(self.ask("How does an eclipse work?")["text"], "A direct answer.")
            lookup.assert_not_called()
        routes = self.script("anything", lambda data: "A direct answer.")
        self.assertEqual(self.ask("What about the thing from before?", session=None)["text"], "A direct answer.")
        self.assertEqual(len(routes), 1)
        turn = make_turn(str(uuid4()), "iphone", "Earlier exchange", "en", "UTC")
        with self.assertRaises(AccessDenied):
            recall_chat(self.store, turn, self.session, "")

    def test_no_match_or_lookup_failure_reaches_the_model_and_retry_keeps_the_context(self):
        for sql, status in (("SELECT * FROM missing_chat_table", "unavailable"), (None, "no_match")):
            with self.subTest(status=status):
                responses = iter(("", "Which jacket did you mean?"))

                def compose(data, status=status, responses=responses):
                    self.assertTrue(data["clarify_transcript"])
                    self.assertEqual(data["recalled_chat"], {"status": status, "exchanges": []})
                    return next(responses)

                identifier = str(uuid4())
                routes = self.script("jacket", compose, route_after="clarify")
                if sql:
                    with self.assertLogs("nex.privacy.chat_recall", "WARNING"), patch(
                        "nex.privacy.chat_recall.BY_TOPIC", sql
                    ):
                        result = self.ask("Could we change that jacket?", identifier=identifier)
                else:
                    result = self.ask("Could we change that jacket?", identifier=identifier)
                self.assertEqual(result["engine"], "model_unavailable")
                self.assertEqual(self.ask("Could we change that jacket?", identifier=identifier)["text"],
                                 "Which jacket did you mean?")
                self.assertEqual(len(routes), 2)

    def test_private_answer_receives_recalled_context_through_approved_handoff(self):
        self.add_exchange("Outline the photo book", "Use the island photos for the cover.")

        def compose(data):
            self.assertIn("island photos", data["previous_answer"])
            return "Use the island photos."

        self.script("photo book", compose)
        reply = self.ask("What about the cover we chose for the photo book?", force=True)
        self.assertEqual(reply["engine"], "private_request")
        self.assertEqual(inbox(self.store.privacy, self.store)["items"][0]["answer"], "Use the island photos.")

    def test_public_replan_discards_recall_based_on_private_followup_history(self):
        self.add_exchange("How should we lay out the kiosk?", "Put the Kestrel banner beside the entrance.")
        self.script("unmatched", lambda data: "Private handoff mentioned Kestrel.")
        self.assertEqual(self.ask("Make a note of that", force=True)["engine"], "private_request")

        def generate(messages, *_args, **_kwargs):
            payload = json.loads(messages[1]["content"])
            if messages[0]["content"].startswith("Select capabilities"):
                if "Private handoff" in json.dumps(payload["history"]) and "recalled_chat" not in payload:
                    return '{"capabilities":["recall_chat"],"language":"en","query":"Kestrel"}'
                return '{"capabilities":["answer"],"language":"en"}'
            self.assertNotIn("recalled_chat", payload)
            self.assertEqual(payload["history"], [])
            return "A direct answer."

        self.assistant.generate_conversation.side_effect = generate
        with patch("nex.privacy.public.recall_chat", wraps=recall_chat) as lookup:
            self.assertEqual(self.ask("How would that look?")["text"], "A direct answer.")
            lookup.assert_called_once()

    def test_terms_in_recalled_exchanges_stay_out_of_public_web_queries(self):
        self.add_exchange("Let's pick a projector", "We chose the Vellucid projector.")

        def compose(data):
            self.assertIn("unavailable", data["tools"]["web"])
            self.assertIn("Vellucid", json.dumps(data["recalled_chat"]))
            return "Can you supply the model name to search?"

        self.script("projector", compose, route_after="web", tools=({"name": "web", "query": "Vellucid projector"},))
        with patch("nex.conversation.reply.fetch") as fetch:
            self.assertEqual(self.ask("Are there newer reviews of that projector we compared?")["text"],
                             "Can you supply the model name to search?")
            fetch.assert_not_called()
