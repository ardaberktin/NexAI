"""Keep only the latest coarse weather position from each opted-in device."""

from time import time
from uuid import uuid4

from .live.weather_location import Coordinates
from .privacy.state import AccessDenied


def enable(store, device):
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("INSERT OR IGNORE INTO shared_weather_locations(device,consent) VALUES(?,?)",
                   (device, uuid4().hex))
        row = db.execute("SELECT consent FROM shared_weather_locations WHERE device=?", (device,)).fetchone()
    return {"consent": row["consent"]}


def consent_from(data):
    consent = data.get("consent")
    if not isinstance(consent, str) or len(consent) != 32:
        raise ValueError("Invalid weather sharing consent.")
    return consent


def clear(store, device, data):
    with store.connect() as db:
        db.execute("DELETE FROM shared_weather_locations WHERE device=? AND consent=?", (device, consent_from(data)))
    return {"shared": False}


def update(store, device, data):
    consent = consent_from(data)
    location = Coordinates.from_reply(data)
    observed = data.get("observed_at")
    now = time()
    if location is None or type(observed) not in (int, float) or not now - 900 <= observed <= now + 60:
        raise ValueError("Send a recent approximate location.")
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT observed FROM shared_weather_locations WHERE device=? AND consent=?",
                         (device, consent)).fetchone()
        if row is None:
            raise AccessDenied("Turn on location sharing in Nex Settings first.")
        if row["observed"] is None or observed >= row["observed"]:
            db.execute("UPDATE shared_weather_locations SET latitude=?,longitude=?,observed=? WHERE device=?",
                       (location.latitude, location.longitude, observed, device))
    return {"shared": True}


def latest(store):
    with store.connect() as db:
        row = db.execute("SELECT latitude,longitude FROM shared_weather_locations WHERE observed IS NOT NULL "
                         "ORDER BY observed DESC, device LIMIT 1").fetchone()
    return Coordinates(row["latitude"], row["longitude"]) if row else None
