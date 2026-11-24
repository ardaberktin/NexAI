"""Read Gmail message previews and Google Calendar events through their APIs."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from html import unescape
from urllib.parse import quote, urlencode

from ..store import excerpt
from .credentials import CALENDARS, EVENTS, GMAIL
from .http import AccountError, request

GMAIL_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages"
CALENDAR_URL = "https://www.googleapis.com/calendar/v3"
INVISIBLE_PADDING = str.maketrans(dict.fromkeys("\u200b\u200c\u200d\u2060\ufeff", " "))


def get(url, token, parameters):
    data, _ = request(url + "?" + urlencode(parameters, doseq=True), token=token)
    if not isinstance(data, dict):
        raise AccountError("Google returned an unexpected response.")
    return data


def clean(value, length):
    return excerpt(" ".join(value.split()), length) if isinstance(value, str) else ""


def gmail_ids(token, intent, limit, *, since=None):
    alternatives = intent.search_variants
    if alternatives:
        groups = ["(" + " ".join(f'"{word}"' for word in value.split()) + ")" for value in alternatives]
        terms = ["{" + " ".join(groups) + "}"]
    else:
        terms = [("in:inbox" if not intent.topic else '"' + intent.topic + '"')]
    if since is not None:
        terms.extend(("in:inbox", f"after:{int(since.timestamp())}"))
    if intent.unread:
        terms.append("is:unread")
    result = get(GMAIL_URL, token, {"maxResults": limit, "q": " ".join(terms), "fields": "messages(id),nextPageToken"})
    return [item["id"] for item in result.get("messages", [])[:limit]]


def gmail_messages(vault, intent, *, since=None):
    token = vault.token("google", {GMAIL})
    limit = 30 if since is not None else 16 if intent.priority else 8
    identifiers = gmail_ids(token, intent, limit, since=since)

    def preview(identifier):
        watching = since is not None
        result = get(GMAIL_URL + "/" + quote(identifier, safe=""), token, {
            "format": "metadata",
            "metadataHeaders": ["Subject"] if watching else ["From", "Date", "Subject"],
            "fields": "payload(headers)" if watching else "snippet,payload(headers)",
        })
        headers = {item["name"].casefold(): item.get("value") for item in result.get("payload", {}).get("headers", [])}
        title = clean(headers.get("subject"), 140) or "No subject"
        if watching:
            return {"id": identifier, "title": title}
        snippet = result.get("snippet")
        if isinstance(snippet, str):
            snippet = unescape(snippet).translate(INVISIBLE_PADDING)
        return {"title": title,
                "from": clean(headers.get("from"), 100), "date": clean(headers.get("date"), 64),
                "preview": clean(snippet, 290)}

    with ThreadPoolExecutor(max_workers=4) as pool:
        return list(pool.map(preview, identifiers))


def event_time(value, zone):
    if value.get("date"):
        return value["date"] + " (all day)"
    try:
        return datetime.fromisoformat(value.get("dateTime", "")).astimezone(zone).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return ""


def calendar_events(vault, start, end, *, watch=False):
    token = vault.token("google", {CALENDARS, EVENTS})
    calendars = get(CALENDAR_URL + "/users/me/calendarList", token, {
        "maxResults": 15, "minAccessRole": "reader",
        "fields": "items(id,summary,primary,selected,hidden),nextPageToken",
    }).get("items", [])[:15]
    selected = [item for item in calendars if item.get("primary") or (item.get("selected") and not item.get("hidden"))]
    calendars = selected[:8] or calendars[:2]

    limit = 50 if watch else 14

    def events(calendar):
        result = get(CALENDAR_URL + "/calendars/" + quote(calendar["id"], safe="") + "/events", token, {
            "timeMin": start.isoformat(), "timeMax": end.isoformat(), "singleEvents": "true", "orderBy": "startTime",
            "maxResults": limit, "showDeleted": "false", "fields": "items(summary,start,end,location),nextPageToken",
        })
        return [{"title": clean(item.get("summary"), 140) or "Untitled event",
                 "start": event_time(item.get("start", {}), start.tzinfo),
                 "end": event_time(item.get("end", {}), start.tzinfo),
                 "calendar": clean(calendar.get("summary"), 65), "location": clean(item.get("location"), 100)}
                for item in result.get("items", [])[:limit]]

    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = [item for calendar in pool.map(events, calendars) for item in calendar]
    ordered = sorted(rows, key=lambda item: item["start"])
    return ordered if watch else ordered[:16]
