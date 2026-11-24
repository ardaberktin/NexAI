import asyncio
import importlib.util
import json
import tempfile
import unittest
from time import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

from conversation_fixtures import model, unlock
from nex.conversation.catalog import WEATHER_SCOPE
from nex.privacy import public, requests
from nex.privacy.chat_history import ChatFollowups
from nex.privacy.policy import request_kind
from nex.store import Store
from nex.weather import Place, Weather


def provider_weather():
    stamp = int(time())
    return {"current": {"time": stamp, "temperature_2m": 18, "apparent_temperature": 16, "weather_code": 0},
            "hourly": {"time": [stamp], "temperature_2m": [18], "weather_code": [0], "precipitation": [0]}}


class PublicWeatherTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(self.enterContext(tempfile.TemporaryDirectory()), 0, 2**20)
        self.assistant = Mock(store=self.store)
        unlock(self.store, "iphone", "phone-session")
        self.assistant.generate_conversation = model(lambda p: [
            {"name": "weather", "period": "current", **({"query": "Paris, France"} if p["question"] == "Paris, France" else {})}
        ])

    def test_only_standalone_weather_can_use_location_hands_free(self):
        for wording in ("How's the weather like outside", "hows the weather like", "What's the weather near me?",
                        "What's the weather like where I am?", "What's the weather in my current location?",
                        "What's the local weather?", "How is the weather in my area?"):
            with self.subTest(wording=wording):
                self.assertEqual(request_kind(wording, "UTC"), "public")
                self.assertEqual(request_kind(wording, "UTC", force=True), "private")
                self.assertEqual(request_kind(wording, "UTC", journal=object()), "private")
        for wording in ("How's the weather outside and what's on my calendar?", "What's the weather at my home address?",
                        "Use Montréal for weather", "Forget my weather city"):
            self.assertEqual(request_kind(wording, "UTC"), "private")

    def test_chat_location_round_trip_is_bound_to_question_and_retryable(self):
        identifier = str(uuid4())
        args = (self.assistant, identifier, "iphone", "hows the weather like", "en", "UTC")
        with patch("nex.weather.read_api", return_value=provider_weather()) as provider:
            self.assertEqual(public.chat(*args, ask_location=True)["engine"], "weather_location_request")
            with self.assertRaises(ValueError):
                public.chat(*args, location={"id": "another", "latitude": 45, "longitude": 20})
            provider.assert_not_called()
            answer = public.chat(*args, location={"id": identifier, "latitude": 48.85632, "longitude": 2.35178})
            self.assertEqual(answer["engine"], "weather")
            evidence = json.loads(answer["text"])
            self.assertEqual(evidence["latest_reading"]["temperature_celsius"], 18)
            self.assertEqual(evidence["area"], "your area")
            parameters = provider.call_args.args[1]
            self.assertEqual((parameters["latitude"], parameters["longitude"]), (48.86, 2.35))
            self.assertEqual(public.chat(*args, ask_location=True), answer)
            provider.assert_called_once()
        self.assertEqual(requests.inbox(self.store.privacy)["items"], [])
        with self.store.connect() as db:
            self.assertFalse(db.execute("SELECT 1 FROM weather_locations").fetchone())
            stored = db.execute("SELECT question,result FROM public_chat WHERE id=?", (identifier,)).fetchone()
            self.assertNotIn("48.86", " ".join(stored))

    def test_missing_location_and_recorded_weather_use_only_this_devices_weather_city(self):
        chosen = Place("Paris", "France", "France", 48.86, 2.35)
        Weather(self.store).remember("mac", chosen)
        cache = ChatFollowups()
        identifier = str(uuid4())
        with patch("nex.weather.read_api", return_value=provider_weather()), patch("nex.weather.place_options", return_value=chosen) as geocoder:
            recorded = public.answer(self.assistant, self.store.privacy, str(uuid4()), "mac",
                                     "What's the weather today?", "en", "UTC", spoken=True)
            self.assertTrue(recorded["public_voice"])
            self.assertIn("Paris", recorded["text"])
            missing = public.chat(self.assistant, identifier, "iphone", "How's the weather?", "en", "UTC",
                                  location={"id": identifier}, followups=cache, session="phone-session")
            self.assertEqual(missing["engine"], "weather_city_needed")
            self.assertNotIn("Paris", missing["text"])
            self.assertEqual(cache.previous("mac", "phone-session"), ())
            self.assertEqual(cache.previous("iphone", "other-session"), ())
            supplied = public.chat(self.assistant, str(uuid4()), "iphone", "Paris, France", "en", "UTC",
                                   followups=cache, session="phone-session", ask_location=True)
            self.assertEqual(supplied["engine"], "weather")
            geocoder.assert_called_once_with("Paris, France", "UTC")
        self.assertIsNone(Weather(self.store).saved("iphone"))
        prompts = [call.args[0][0]["content"] for call in self.assistant.generate_conversation.call_args_list]
        self.assertEqual(prompts.count(WEATHER_SCOPE), 1)


@unittest.skipUnless(importlib.util.find_spec("pipecat"), "requires the live speech virtual environment")
class LiveWeatherTests(unittest.IsolatedAsyncioTestCase):
    async def test_unverified_voice_gets_weather_on_each_device_and_private_requests_stay_gated(self):
        from nex.live.dialogue import Dialogue
        from nex.live.session import Session
        from nex.live_memory import LiveMemory

        directory = self.enterContext(tempfile.TemporaryDirectory())
        store = Store(directory, 0, 2**20)
        dialogue = Dialogue(LiveMemory(directory), "/unused", None)
        self.addAsyncCleanup(dialogue.close)
        generate = model(lambda p: [{"name": "calendar"}] if "calendar" in p["question"] else [{"name": "weather", "period": "current"}])
        dialogue.complete = AsyncMock(side_effect=lambda _, messages, *a, **kw: generate(messages))
        speech = Mock()
        speech.listen.return_value.vad.num_frames_required.return_value = 512
        speech.acknowledgments = {"en": bytes(100), "tr": bytes(100)}
        speech.speak.side_effect = lambda *_: iter([bytes(100)])
        events = []

        async def receive(event):
            events.append(event)
            if event["kind"] == "weather_location_request":
                session.control_result(json.dumps({"kind": "weather_location_result", "id": event["id"],
                                                   "latitude": 48.85632, "longitude": 2.35178}))

        socket = SimpleNamespace(send_json=receive, send_bytes=AsyncMock())
        with patch("nex.weather.read_api", return_value=provider_weather()) as provider:
            for device in ("iphone", "mac"):
                session = Session(socket, device, "en", "UTC", False, speech, dialogue, Mock())
                session.weather_location_supported = True
                events.clear()
                await session.begin_with_question("How's the weather like outside")
                await asyncio.wait_for(session.playback, 3)
                result = events[-1]["reply"]
                self.assertEqual(result["engine"], "weather")
                self.assertTrue(result["public_voice"])
                self.assertEqual(json.loads(result["text"])["area"], "your area")
                self.assertEqual(sum(e["kind"] == "weather_location_request" for e in events), 1)
                self.assertIsNone(session.weather_location)
                events.clear()
                await session.begin_with_question("What's on my calendar?")
                await asyncio.wait_for(session.playback, 3)
                self.assertEqual(events[-1]["reply"]["engine"], "private_request")
                self.assertFalse(any(e["kind"] == "weather_location_request" for e in events))
            self.assertEqual(provider.call_count, 2)
        self.assertTrue(all(item["question"] == "What's on my calendar?" for item in requests.inbox(store.privacy)["items"]))
