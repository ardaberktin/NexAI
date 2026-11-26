"""Choose bounded activity evidence for ordinary conversation and explicit recall."""

import re
from datetime import UTC, datetime, time, timedelta

from ..conversation_intent import words
from ..recall_dates import recall_dates, time_key, time_zone
from ..store import normalized, query_terms
from ..workspace_features import is_enabled
from .timeline import activity_passage, timeline

ACTION = (r"(?:doing|do|done|watch(?:ing|ed)?|play(?:ing|ed)?|scroll(?:ing|ed)?|us(?:e|ing|ed)|"
          r"listen(?:ing|ed)?|work(?:ing|ed)? on|spend(?:ing)?|spent|on (?:my |the )?"
          r"(?:phone|ps5|playstation|tiktok|instagram|computer))")
PERSONAL_ACTIVITY = re.compile(
    rf"\b(?:am|was|were|did|have|had|are) (?:i|we) (?:been |just |last |currently |still |actually )?{ACTION}\b|"
    rf"\b(?:what|when|which|how long|how much time|whether)\b.{{0,48}}\b(?:i|we) "
    rf"(?:(?:have been |was |were |am |usually ){ACTION}|watched|played|used|spent|did)\b|"
    r"\b(?:my activities|my activity|my screen time|my day look like|how i spent my|what i got up to|"
    r"ne yapiyorum|ne yapiyordum|ne yaptim|ne izliyordum|ne oynuyordum)\b"
)
PAST = re.compile(r"\b(?:did|was|were|had|have|earlier|before|last|ago|history|spent|yaptim|yapiyordum)\b")
FRESH = timedelta(seconds=90)
AT_TIME = re.compile(r"\bat (\d{1,2})(?::([0-5]\d))?\s*(am|pm)\b|\bat ([01]?\d|2[0-3]):([0-5]\d)\b")
TOPICS = {"tiktok": "tiktok", "instagram": "instagram", "ps5": "ps5", "playstation": "ps5",
          "movie": "movie", "show": "show", "phone": "phone", "game": "gaming", "music": "music"}
CURRENT_QUESTION = re.compile(
    r"(?:(?:hey )?nex )?(?:what (?:am i|are we)|(?:can you tell what|what do you think) "
    r"(?:i am|i m|we are|we re)) (?:currently )?doing(?: (?:right )?now| at the moment)?(?: nex)?"
)


def current_question(query):
    return bool(CURRENT_QUESTION.fullmatch(words(query)))


def activity_question(query):
    return current_question(query) or bool(PERSONAL_ACTIVITY.search(normalized(query)))


def recent_periods(store, anchor, exclude):
    bounds = time_key(anchor - FRESH), time_key(anchor)
    periods = timeline(store, bounds, exclude=exclude, as_of=anchor)["items"]
    # A fresh period on each microphone supersedes previous guesses, including after silence.
    return list({p["device"]: p for p in periods}.values())


def evidence_for_periods(periods, query, zone, limit=5):
    terms = query_terms(query)

    def rank(period):
        title = normalized(period["title"] + " " + period["category"])
        detail = normalized(period["detail"])
        return (sum(2 * (term in title) + (term in detail) for term in terms),
                period["category"] != "unknown", period["ended"])

    chosen = sorted(periods, key=rank, reverse=True)[:limit]
    return [activity_passage(period, zone) for period in sorted(chosen, key=lambda p: p["started"])]


def historical_window(store, query, anchor):
    topic = next((category for word, category in TOPICS.items() if re.search(rf"\b{word}\b", query)), None)
    if topic and "last" in query:
        with store.connect() as db:
            row = db.execute("SELECT started FROM activity_clips WHERE category=? AND started<? "
                             "ORDER BY started DESC LIMIT 1", (topic, time_key(anchor))).fetchone()
        if row:
            last = datetime.fromisoformat(row["started"])
            return time_key(last - timedelta(minutes=20)), time_key(min(last + timedelta(seconds=65), anchor))
    return time_key(anchor - timedelta(days=1)), time_key(anchor)


def activity_evidence(store, query, zone, asked_at=None, *, exclude=None, scope=None):
    if not is_enabled(store, "activities"):
        return []
    anchor = asked_at or datetime.now(UTC)
    tz = time_zone(zone)
    explicit = activity_question(query)
    if explicit and scope is None:
        scope = recall_dates(query, zone, anchor)
    clean = normalized(query)
    history = explicit and bool(PAST.search(clean))
    clock = AT_TIME.search(clean) if explicit else None
    if scope:
        bounds = scope.bounds[0], min(scope.bounds[1], time_key(anchor))
    elif explicit and "last night" in clean:
        midnight = datetime.combine(anchor.astimezone(tz).date(), time(), tz)
        bounds = time_key(midnight - timedelta(hours=6)), time_key(min(anchor, midnight + timedelta(hours=5)))
    elif history:
        bounds = historical_window(store, clean, anchor)
    elif not clock:
        return evidence_for_periods(recent_periods(store, anchor, exclude), query, tz)
    if clock:
        if clock[3] and not 1 <= int(clock[1]) <= 12:
            return []
        hour = int(clock[4]) if clock[4] else int(clock[1]) % 12 + (12 if clock[3] == "pm" else 0)
        day = datetime.fromisoformat(scope.local_dates[0]).date() if scope else anchor.astimezone(tz).date()
        at = datetime.combine(day, time(hour, int(clock[2] or clock[5] or 0)), tz)
        if not scope and at > anchor and history:
            at -= timedelta(days=1)
        bounds = time_key(at), time_key(min(at + timedelta(minutes=1), anchor))
    if bounds[0] >= bounds[1]:
        return []
    periods = timeline(store, bounds, exclude=exclude, as_of=anchor)["items"]
    return evidence_for_periods(periods, query, tz)
