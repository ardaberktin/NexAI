"""Read current conditions for an explicit city or approximate phone location."""

import http.client
import json
import math
from dataclasses import asdict, dataclass
from time import monotonic, time
from urllib.parse import urlencode, urlsplit

from .shared_weather import latest as shared_weather_location
from .store import normalized
from .web_transport import WebReadError, body_text, open_response

GEOCODING = "https://geocoding-api.open-meteo.com/v1/search"
CONDITIONS = "https://api.open-meteo.com/v1/forecast"

MESSAGES = {
    "en": {
        "city": "Which city should I check?",
        "location_missing": "I couldn't get this device's location.",
        "nearby": "your area",
        "more": "Which city should I check, including the state, province, or country?",
        "failed": "I could not get current weather. Try again in a moment.",
        "forgot": "I forgot the weather city for this device.",
        "current": "It is {temperature} degrees Celsius in {city}, with {condition}.",
        "feels": "It feels like {temperature} degrees.",
    },
    "tr": {
        "city": "Hava durumu için hangi şehri kullanayım? Bu cihaz için hatırlayacağım.",
        "location_missing": "iPhone konumunu alamadım.",
        "location_city": "iPhone Ayarlarından Nex için konuma izin ver veya kullanacağım şehri söyle.",
        "nearby": "bulunduğun yer",
        "more": "Şehri belirleyemedim. Şehirle birlikte bölgesini veya ülkesini söyle.",
        "failed": "Güncel hava durumunu alamadım. Biraz sonra tekrar dene.",
        "forgot": "Bu cihazın hava durumu şehrini unuttum.",
        "current": "{city} için hava {condition}, sıcaklık {temperature} derece.",
        "feels": "Hissedilen sıcaklık {temperature} derece.",
    },
}
CONDITION_GROUPS = (
    ((0,), "clear skies", "açık"),
    ((1,), "mostly clear skies", "çoğunlukla açık"),
    ((2,), "partly cloudy skies", "parçalı bulutlu"),
    ((3,), "overcast skies", "kapalı"),
    ((45, 48), "fog", "sisli"),
    ((51, 53, 55), "drizzle", "çiseli"),
    ((56, 57, 66, 67), "freezing rain", "donan yağmurlu"),
    ((61, 63, 65, 80, 81, 82), "rain", "yağmurlu"),
    ((71, 73, 75, 77, 85, 86), "snow", "karlı"),
    ((95, 96, 99), "thunderstorms", "gök gürültülü"),
)


@dataclass(frozen=True)
class WeatherAnswer:
    text: str
    needs_city: bool = False
    sourced: bool = False

    def reply(self, language, emit=None, *, recorded=False):
        text = self.text
        if recorded and self.needs_city and language == "en":
            text = "Tell me which city to check by saying, for example, ‘What’s the weather in Vancouver?’"
        if emit is not None:
            emit(text, language)
        if self.sourced:
            label = "Kaynak" if language == "tr" else "Source"
            text += f"\n\n{label}: Open-Meteo."
        return {"text": text, "language": language, "sources": [],
                "engine": "weather_city_needed" if self.needs_city else "weather"}


@dataclass(frozen=True)
class Place:
    name: str
    region: str
    country: str
    latitude: float
    longitude: float

    @property
    def label(self):
        return ", ".join(dict.fromkeys(value for value in (self.name, self.region or self.country) if value))


def read_api(endpoint, parameters):
    # Fixed provider endpoints. Send only a city search or coordinates.
    url = urlsplit(endpoint + "?" + urlencode(parameters))
    deadline = monotonic() + 5
    connection, response = open_response(url, deadline, accept="application/json")
    try:
        if response.status != 200 or response.headers.get_content_type() != "application/json":
            raise WebReadError("Weather provider returned an unexpected response.")
        result = json.loads(body_text(response, connection, deadline))
        if not isinstance(result, dict):
            raise TypeError("Weather provider returned invalid data.")
        return result
    finally:
        response.close()
        connection.close()


def place_options(query, zone):
    name, *qualifiers = (part.strip() for part in query.split(","))
    place = find_place(name, qualifiers, zone)
    if place is not None or qualifiers:
        return place
    # Spoken city and region names often arrive without a comma.
    parts = name.split()
    for suffix_count in range(1, min(3, len(parts))):
        prefix, qualifier = " ".join(parts[:-suffix_count]), " ".join(parts[-suffix_count:])
        if place := find_place(prefix, [qualifier], zone):
            return place
    return None


def find_place(name, qualifiers, zone):
    rows = read_api(GEOCODING, {"name": name, "count": 10, "language": "en"}).get("results", [])
    exact = [row for row in rows if normalized(row["name"]) == normalized(name)]
    qualified = [row for row in exact if all(matches_region(row, term) for term in qualifiers)]
    if len(qualified) > 1:
        local = [row for row in qualified if row.get("timezone") == zone]
        if len(local) == 1 and not qualifiers:
            qualified = local
    if len(qualified) != 1:
        return None
    row = qualified[0]
    latitude, longitude = float(row["latitude"]), float(row["longitude"])
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        raise ValueError("Invalid weather coordinates.")
    return Place(row["name"], row.get("admin1", ""), row.get("country", ""), latitude, longitude)


def matches_region(row, term):
    region = normalized(row.get("admin1", ""))
    country = normalized(row.get("country", ""))
    code = normalized(row.get("country_code", ""))
    initials = "".join(word[0] for word in region.split())
    def compact(value):
        return value.replace(".", "").replace(" ", "")
    return compact(normalized(term)) in {compact(value) for value in (region, country, code, initials) if value}


def current_conditions(place):
    row = read_api(CONDITIONS, {
        "latitude": round(place.latitude, 3), "longitude": round(place.longitude, 3),
        "current": "temperature_2m,apparent_temperature,weather_code",
        "timeformat": "unixtime", "forecast_days": 1,
    })["current"]
    temperature, feels_like, observed = (float(row[key]) for key in ("temperature_2m", "apparent_temperature", "time"))
    if not all(map(math.isfinite, (temperature, feels_like, observed))) or abs(time() - observed) > 90 * 60:
        raise ValueError("Incomplete or stale current weather.")
    code = int(row["weather_code"])
    return code, round(temperature), round(feels_like)


class Weather:
    def __init__(self, store):
        self.store = store

    def saved(self, device):
        with self.store.connect() as db:
            row = db.execute("SELECT place FROM weather_locations WHERE device=?", (device,)).fetchone()
        return Place(**json.loads(row["place"])) if row else None

    def remember(self, device, place):
        with self.store.connect() as db:
            if place is None:
                db.execute("DELETE FROM weather_locations WHERE device=?", (device,))
            else:
                db.execute(
                    "INSERT INTO weather_locations(device,place) VALUES(?,?) "
                    "ON CONFLICT(device) DO UPDATE SET place=excluded.place",
                    (device, json.dumps(asdict(place), ensure_ascii=False)),
                )

    def locate(self, request, device, zone, language, coordinates=None, location_requested=False):
        messages = MESSAGES[language]
        shared = shared_weather_location(self.store) if request.is_local and coordinates is None else None
        missing_location = request.is_local and location_requested and coordinates is None and shared is None
        prefix = messages["location_missing"] + " " if missing_location else ""
        if request.city:
            place = place_options(request.city, zone)
            if place is None:
                return None, WeatherAnswer(messages["more"], needs_city=True)
        elif coordinates is not None and request.is_local:
            place = Place(messages["nearby"], "", "", coordinates.latitude, coordinates.longitude)
        elif shared:
            place = Place("your last shared area" if language == "en" else messages["nearby"], "", "",
                          shared.latitude, shared.longitude)
        elif (place := self.saved(device)) is None:
            prompt = messages.get("location_city", messages["city"]) if missing_location else messages["city"]
            return None, WeatherAnswer(prefix + prompt, needs_city=True)
        return place, WeatherAnswer(prefix)

    def answer(self, request, device, zone, language, *, coordinates=None, location_requested=False):
        messages = MESSAGES[language]
        if request.forget:
            self.remember(device, None)
            return WeatherAnswer(messages["forgot"])
        prefix = ""
        try:
            place, status = self.locate(request, device, zone, language, coordinates, location_requested)
            if place is None:
                return status
            prefix = status.text
            if request.remember:
                self.remember(device, place)
            code, temperature, feels_like = current_conditions(place)
            condition = next(tr if language == "tr" else en for codes, en, tr in CONDITION_GROUPS if code in codes)
        except (OSError, http.client.HTTPException, WebReadError, ValueError, TypeError, KeyError, StopIteration):
            return WeatherAnswer(prefix + messages["failed"])
        text = prefix + messages["current"].format(condition=condition, temperature=temperature, city=place.label)
        if abs(feels_like - temperature) >= 2:
            text += " " + messages["feels"].format(temperature=feels_like)
        return WeatherAnswer(text, sourced=True)
