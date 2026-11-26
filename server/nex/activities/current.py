"""Answer a direct question about the present from the latest attributed observations."""

from datetime import UTC, datetime

from ..recall_dates import time_key
from .brand_cues import BRANDS, MENU_NAMES, interface_quote
from .recall import FRESH, current_question, recent_periods

PHRASES = {
    "ps5": "playing on a PS5", "media": "watching or listening to media",
    "livestream": "watching a livestream",
    "conversation": "having a conversation", "housework": "doing housework",
}


def current_snapshot(store, at=None):
    anchor = at or datetime.now(UTC)
    fields = ("started", "ended", "device", "category", "title", "confidence")
    items = []
    for period in recent_periods(store, anchor, None):
        item = {key: period[key] for key in fields}
        if item["category"] == "speech" or (item["category"] == "unknown" and period["sounds"]):
            # Give Chat a display category for audible evidence with no identified activity.
            item["category"] = "sound"
        items.append(item)
    return {"items": items, "as_of": time_key(anchor), "fresh_for": int(FRESH.total_seconds())}


def describe(period):
    category, basis = period["category"], period["basis"]
    if category == "unknown":
        return "I couldn’t identify your activity from the latest recording."
    if category == "speech":
        return "I can hear speech nearby. The recording leaves your activity unclear."
    if category == "nex":
        return "Your latest recorded activity was a conversation with me."
    title = period["title"]
    phrase = PHRASES.get(category, title[:1].lower() + title[1:])
    if basis == "Shared Mac screen":
        return f"Your shared Mac screen suggests you’re {phrase}."
    if basis == "Device playback":
        return f"Active playback on your device suggests you’re {phrase}."
    if basis == "Recorded speech":
        detail = period["detail"]
        if category in BRANDS and interface_quote(category, detail):
            return f"It sounds like you’re {phrase}. I heard {MENU_NAMES[category]}."
        return f"It sounds like you’re {phrase}. The recording picked up “{detail.rstrip('.')}.”"
    sounds = period["detail"].removeprefix("Heard ").rstrip(".")
    return f"It sounds like you’re {phrase}. I heard {sounds.lower()}."


def current_activity_reply(store, query, language, asked_at=None, *, exclude=None):
    if language != "en" or not current_question(query):
        return None
    periods = sorted(recent_periods(store, asked_at or datetime.now(UTC), exclude),
                     key=lambda period: period["ended"], reverse=True)
    observed = ([p for p in periods if p["category"] not in ("unknown", "speech")]
                or [p for p in periods if p["category"] == "speech"][:1] or periods[:1])
    by_category = {}
    for period in observed:
        by_category.setdefault(period["category"], period)
    text = " ".join(map(describe, by_category.values())) or "I don’t have a recent recording to tell what you’re doing."
    return {"text": text, "language": language, "sources": [p["id"] for p in by_category.values()],
            "engine": "archive_search"}
