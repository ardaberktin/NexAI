import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from nex.live.weather_location import Coordinates
from nex.store import Store
from nex.weather import CONDITIONS, GEOCODING, Place, Weather
from nex.weather_intent import (
    WeatherRequest,
    repair_weather_transcript,
    weather_city,
    weather_request,
)


class WeatherTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.weather = Weather(Store(directory, 0, 2**30))
        self.zone = "America/Vancouver"
        self.places = [
            {"name": "Vancouver", "admin1": "British Columbia", "country": "Canada",
             "country_code": "CA", "timezone": "America/Vancouver", "latitude": 49.25, "longitude": -123.12},
            {"name": "Vancouver", "admin1": "Washington", "country": "United States",
             "country_code": "US", "timezone": "America/Los_Angeles", "latitude": 45.63, "longitude": -122.66},
        ]

    def test_weather_intent_and_city_privacy(self):
        for question in ("How’s the weather like?", "How is the weather like outside", "What's the weather like, Nex?",
                         "What's the weather near me?", "What's the weather like where I am?",
                         "What's the weather in my current location?"):
            self.assertEqual(weather_request(question), WeatherRequest())
        self.assertEqual(weather_request("What’s the weather in Vancouver, BC today?"), WeatherRequest("Vancouver, BC"))
        self.assertEqual(weather_request("Use Montréal for weather"), WeatherRequest("Montréal", remember=True))
        for text in ("I don't know", "My exact address", "443 Small Street", "What's the weather in my home?"):
            self.assertIsNone(weather_city(text))
        self.assertIsNone(weather_request("What was the weather like yesterday?"))
        self.assertIsNone(weather_request("How's it with the Lakota sign?"))

    async def test_actual_misheard_wording_requires_support_from_the_second_pass(self):
        verifier = AsyncMock()
        verifier.recover.return_value = "Nex shows it with a like outside."
        spoken = "Nex. How's it with the Lakot sign?"
        fixed = await repair_weather_transcript(verifier, b"pcm", "auto", spoken)
        self.assertEqual(weather_request(fixed), WeatherRequest())
        verifier.recover.assert_awaited_once_with(b"pcm", "auto", weather=True)

        verifier.recover.return_value = "Some other words"
        self.assertEqual(await repair_weather_transcript(verifier, b"pcm", "auto", spoken), "")
        self.assertEqual(await repair_weather_transcript(verifier, b"pcm", "auto", "How’s the weather?"),
                         "How’s the weather?")

    def test_remember_explicit_city_only_for_the_device_and_report_fresh_conditions(self):
        def api(endpoint, params):
            if endpoint == GEOCODING:
                self.assertEqual(params["name"], "Vancouver")
                return {"results": self.places}
            self.assertEqual(endpoint, CONDITIONS)
            self.assertEqual(params["latitude"], 49.25)
            self.assertEqual(params["longitude"], -123.12)
            return {"current": {"time": 100_000, "temperature_2m": 8.2,
                                "apparent_temperature": 5.1, "weather_code": 61}}

        with patch("nex.weather.read_api", side_effect=api) as requested, patch("nex.weather.time", return_value=100_120):
            location = self.weather.answer(WeatherRequest(), "iphone", self.zone, "en")
            self.assertTrue(location.needs_city)
            self.assertIn("Which city", location.text)
            requested.assert_not_called()

            answer = self.weather.answer(WeatherRequest("Vancouver, BC", remember=True), "iphone", self.zone, "en")
            self.assertFalse(answer.needs_city)
            self.assertTrue(answer.sourced)
            self.assertIn("8 degrees Celsius in Vancouver", answer.text)
            self.assertIn("rain", answer.text)
            self.assertIn("feels like 5", answer.text)
            self.assertEqual(self.weather.answer(WeatherRequest(), "iphone", self.zone, "en"), answer)
            self.assertTrue(self.weather.answer(WeatherRequest(), "mac", self.zone, "en").needs_city)
            self.weather.answer(WeatherRequest(forget=True), "iphone", self.zone, "en")
            self.assertIsNone(self.weather.saved("iphone"))

    def test_ambiguous_city_and_stale_weather_never_become_current_conditions(self):
        rows = {"results": self.places}
        stale = {"current": {"time": 100, "temperature_2m": 8, "apparent_temperature": 8, "weather_code": 0}}
        with patch("nex.weather.read_api", side_effect=[rows, rows, stale]), patch("nex.weather.time", return_value=100_000):
            ambiguous = self.weather.answer(WeatherRequest("Vancouver"), "iphone", "UTC", "en")
            self.assertIn("province", ambiguous.text)
            out_of_date = self.weather.answer(WeatherRequest("Vancouver"), "iphone", self.zone, "en")
            self.assertIn("could not get current weather", out_of_date.text)
            self.assertFalse(out_of_date.sourced)

    def test_fresh_location_takes_priority_without_overwriting_saved_city(self):
        home = Place("Vancouver", "British Columbia", "Canada", 49.25, -123.12)
        self.weather.remember("iphone", home)
        with patch("nex.weather.current_conditions", side_effect=[(2, 18, 18), (3, 24, 24)]) as conditions:
            for coordinate in (Coordinates(48.86, 2.35), Coordinates(35.68, 139.69)):
                result = self.weather.answer(WeatherRequest(), "iphone", self.zone, "en",
                                             coordinates=coordinate, location_requested=True)
                self.assertIn("in your area", result.text)
                self.assertTrue(result.sourced)
                queried_place, = conditions.call_args.args
                self.assertEqual((queried_place.latitude, queried_place.longitude),
                                 (coordinate.latitude, coordinate.longitude))
        self.assertEqual(self.weather.saved("iphone"), home)

    def test_unavailable_location_labels_saved_city_or_prompts_when_none_is_saved(self):
        query = WeatherRequest()
        with patch("nex.weather.current_conditions", return_value=(2, 10, 10)) as conditions:
            missing = self.weather.answer(query, "iphone", self.zone, "en", location_requested=True)
            self.assertTrue(missing.needs_city)
            self.assertIn("Which city", missing.text)
            conditions.assert_not_called()
            self.weather.remember("iphone", Place("Vancouver", "British Columbia", "Canada", 49.25, -123.12))
            saved = self.weather.answer(query, "iphone", self.zone, "en", location_requested=True)
            self.assertIn("couldn't get this device's location", saved.text)
            self.assertIn("in Vancouver", saved.text)
