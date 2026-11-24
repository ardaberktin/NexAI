import tempfile
import unittest
from time import time
from unittest.mock import patch

from nex import shared_weather
from nex.http_api import recorder_operation
from nex.live.weather_location import Coordinates, WeatherLocationTurn
from nex.privacy.state import AccessDenied
from nex.store import Store
from nex.weather import Place, Weather
from nex.weather_intent import WeatherRequest


class WeatherLocationTests(unittest.IsolatedAsyncioTestCase):
    async def test_matches_only_current_turn_and_rounds_coordinates(self):
        turn = WeatherLocationTurn("current")
        turn.receive({"id": "old", "latitude": 49.28564, "longitude": -123.12521})
        self.assertFalse(turn.reply.done())
        turn.receive({"id": "current", "latitude": 49.28564, "longitude": -123.12521})
        self.assertEqual(await turn.wait(), Coordinates(49.29, -123.13))
        turn.receive({"id": "current", "latitude": 0, "longitude": 0})
        self.assertEqual(turn.reply.result(), Coordinates(49.29, -123.13))

    async def test_unavailable_location_and_timeout_let_weather_fall_back(self):
        denied = WeatherLocationTurn("denied")
        denied.receive({"id": "denied"})
        self.assertIsNone(await denied.wait())
        expired = WeatherLocationTurn("expired")
        self.assertIsNone(await expired.wait(seconds=0))
        expired.receive({"id": "expired", "latitude": 49, "longitude": -123})
        self.assertTrue(expired.reply.cancelled())

    async def test_rejects_non_numeric_incomplete_and_invalid_coordinates(self):
        turn = WeatherLocationTurn("current")
        for latitude, longitude in ((True, 0), (49, None), ("49", 0), (91, 0), (0, -181), (float("nan"), 0)):
            with self.subTest(latitude=latitude, longitude=longitude), self.assertRaises(ValueError):
                turn.receive({"id": "current", "latitude": latitude, "longitude": longitude})
        self.assertFalse(turn.reply.done())


class SharedWeatherTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = Store(folder.name, 0, 2**20)

    def test_consent_requires_unlock_and_fixes_are_device_bound(self):
        base = "/v1/workspace/weather-location"
        self.assertFalse(recorder_operation("PUT", base))
        self.assertTrue(recorder_operation("PUT", base + "/fix"))
        consent = shared_weather.enable(self.store, "iphone")
        fix = consent | {"latitude": 49.28564, "longitude": -123.12521, "observed_at": time()}
        with self.assertRaises(AccessDenied):
            shared_weather.update(self.store, "mac", fix)
        shared_weather.update(self.store, "iphone", fix)
        self.assertEqual(shared_weather.latest(self.store), Coordinates(49.29, -123.13))
        shared_weather.update(self.store, "iphone", fix | {"latitude": 50, "observed_at": time() - 20})
        self.assertEqual(shared_weather.latest(self.store), Coordinates(49.29, -123.13))
        shared_weather.clear(self.store, "iphone", consent)
        self.assertIsNone(shared_weather.latest(self.store))
        self.assertNotEqual(shared_weather.enable(self.store, "iphone"), consent)
        with self.assertRaises(AccessDenied):
            shared_weather.update(self.store, "iphone", fix)

    def test_weather_uses_the_latest_shared_area_and_falls_back_after_revocation(self):
        weather = Weather(self.store)
        weather.remember("mac", Place("Victoria", "British Columbia", "Canada", 48.4, -123.4))
        consent = shared_weather.enable(self.store, "iphone")
        shared_weather.update(self.store, "iphone", consent | {"latitude": 49.3, "longitude": -123.1,
                                                                 "observed_at": time()})
        with patch("nex.weather.current_conditions", return_value=(0, 12, 12)) as current:
            answer = weather.answer(WeatherRequest(), "mac", "America/Vancouver", "en")
            self.assertIn("your last shared area", answer.text)
            self.assertEqual(current.call_args.args[0].latitude, 49.3)
            shared_weather.clear(self.store, "iphone", consent)
            answer = weather.answer(WeatherRequest(), "mac", "America/Vancouver", "en")
            self.assertIn("Victoria", answer.text)
