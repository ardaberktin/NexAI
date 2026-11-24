"""Hourly weather evidence for the requested local day, with a recording-time cutoff."""
import math
from collections import Counter
from datetime import datetime

from .. import weather
from ..recall_dates import time_zone


def condition_name(code):
    return next((en for codes, en, _ in weather.CONDITION_GROUPS if code in codes), None)


def latest_reading(row, day, zone, anchor):
    stamp, temperature = row.get("time"), row.get("temperature_2m")
    if not all(isinstance(val, (int, float)) and math.isfinite(val) for val in (stamp, temperature)):
        return None
    if not 0 <= anchor.timestamp() - stamp <= 90 * 60:
        return None
    at = datetime.fromtimestamp(stamp, time_zone(zone))
    if at.date() != day:
        return None
    reading = {"as_of": at.strftime("%H:%M"), "temperature_celsius": temperature,
               "condition": condition_name(row.get("weather_code"))}
    feels = row.get("apparent_temperature")
    if isinstance(feels, (int, float)) and math.isfinite(feels):
        reading["feels_like_celsius"] = feels
    return reading


def hourly(place, day, zone, period, anchor):
    parameters = {
        "latitude": round(place.latitude, 3), "longitude": round(place.longitude, 3),
        "timezone": zone, "start_date": day.isoformat(), "end_date": day.isoformat(),
        "hourly": "temperature_2m,weather_code,precipitation", "timeformat": "unixtime",
    }
    if period == "current":
        parameters["current"] = "temperature_2m,apparent_temperature,weather_code"
    response = weather.read_api(weather.CONDITIONS, parameters)
    row = response["hourly"]
    hours = []
    for stamp, temperature, code, rain in zip(row["time"], row["temperature_2m"], row["weather_code"],
                                              row["precipitation"], strict=True):
        at = datetime.fromtimestamp(stamp, time_zone(zone))
        if at.date() != day or (period != "forecast" and at > anchor):
            continue
        if temperature is None or not math.isfinite(temperature):
            continue
        hours.append((at.strftime("%H:%M"), temperature, condition_name(code), rain))
    report = {"source": "Open-Meteo", "area": place.label, "date": day.isoformat(), "timezone": zone,
              "period": "forecast" if period == "forecast" else "past"}
    if period == "current" and (latest := latest_reading(response.get("current") or {}, day, zone, anchor)):
        report["latest_reading"] = latest
    if not hours and "latest_reading" not in report:
        raise ValueError("No hourly weather for the requested period.")
    if hours:
        report["intervals"] = [summary(hours[i:i + 3], interval=True) for i in range(0, len(hours), 3)]
        report.update(summary(hours))
    return report


def summary(hours, *, interval=False):
    rain = [item[3] for item in hours]
    complete = all(isinstance(value, (int, float)) and math.isfinite(value) for value in rain)
    counts = Counter(item[2] for item in hours if item[2])
    peak = max(counts.values(), default=0)
    result = {"hours_covered": [hours[0][0], hours[-1][0]], "low_celsius": min(item[1] for item in hours),
              "high_celsius": max(item[1] for item in hours),
              "prevailing_conditions": [condition for condition, count in counts.items() if count == peak],
              "precipitation_mm": round(sum(rain), 1) if complete else None}
    if not interval:
        result["hours_by_condition"] = dict(counts.most_common())
        result["hours_with_measurable_precipitation"] = sum(value > 0 for value in rain) if complete else None
    return result
