"""Use the observed sounds and screens directly on days without independent spoken material."""

from datetime import datetime

from .brand_cues import BRANDS, MENU_NAMES, interface_quote, menu_and_speech
from .timeline import activity_passage


def briefing_observation(period, zone):
    passage = activity_passage(period, zone)
    stamp = datetime.fromisoformat(period["started"]).astimezone(zone).strftime("%H:%M %Z")
    category, basis, detail = period["category"], period["basis"], period["detail"]
    if basis == "Recorded speech" and category in BRANDS and interface_quote(category, detail):
        note = f"At {stamp}, the recording picked up {MENU_NAMES[category]}."
    elif basis == "Room audio" and detail.startswith("Heard "):
        note = f"At {stamp}, the recording picked up {detail[6:].lower()}"
    else:
        activity = period["title"][:1].lower() + period["title"][1:]
        note = f"At {stamp}, {basis.lower()} suggested {activity}."
    return passage | {"category": category, "note": note}


def observation_briefing(rows, activities):
    if not activities or any(menu_and_speech(row["transcript"] or "")[1] for row in rows):
        return None
    distinct = {}
    for item in activities:
        distinct.setdefault(item["category"], item)
    events = list(distinct.values())
    highlights = [{"kind": "key_moment", "text": event["note"], "source": event["id"]} for event in events[:4]]
    return {"summary": " ".join(event["note"] for event in events[:2]), "highlights": highlights}
