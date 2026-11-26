"""Compose a dated agenda from independently available, read-only sources."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from ..recall_dates import time_key
from ..workspace_features import enabled
from .intent import AccountIntent, date_window
from .read import account_reply, prepare_account


def agenda_evidence(store, query, zone, reference, *, limit=12):
    start, end = date_window(query, zone, reference)
    with store.connect() as db:
        scheduled = db.execute(
            """SELECT title,scheduled_at FROM goals WHERE interval_minutes=0 AND status='active'
               AND scheduled_at>=? AND scheduled_at<? ORDER BY scheduled_at, id LIMIT 20""",
            (time_key(start), time_key(end)),
        ).fetchall() if enabled(db, "goals") else []
    items = [(datetime.fromisoformat(row["scheduled_at"]).astimezone(start.tzinfo).strftime("%Y-%m-%d %H:%M"),
              "Nex goal", row["title"]) for row in scheduled]
    unavailable = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        calls = [pool.submit(prepare_account, store.root, AccountIntent(provider, False), query, zone, reference)
                 for provider in ("calendar", "ticktick")]
        for call in calls:
            label, rows, status = call.result()
            if status and not status.startswith("I found no matching items"):
                unavailable.append(status)
            for row in rows:
                items.append((row.get("due") or row.get("start") or "", label, row["title"]))
    date = start.strftime("%A, %B %d").replace(" 0", " ")
    if len(unavailable) == 2 and all(message.startswith("Connect ") for message in unavailable):
        unavailable = ["Connect Google Calendar and TickTick in Settings to include events and tasks."]
    rows = []
    for stamp, label, title in sorted(items)[:limit]:
        when = stamp.removeprefix(start.date().isoformat()).strip()
        rows.append({"time": "all day" if when == "(all day)" else when, "source": label, "title": title})
    return {"date": date, "items": rows, "more": max(0, len(items) - limit), "unavailable": unavailable}


def daily_agenda(store, query, zone, reference, language, *, spoken, emit=None):
    agenda = agenda_evidence(store, query, zone, reference, limit=5 if spoken else 12)
    date = agenda["date"]
    if agenda["items"]:
        lines = [f"For {date}, here's what's on your agenda."]
        for item in agenda["items"]:
            when, label, title = item["time"], item["source"], item["title"]
            lines.append(f"{when + ' — ' if when else ''}{title} ({label}).")
        if more := agenda["more"]:
            lines.append(f"There are {more} more items in the results.")
    else:
        lines = [f"I found no items for {date} in the sources I could check."]
    lines.extend(agenda["unavailable"])
    return account_reply("agenda", [], "", " ".join(lines), language, spoken=spoken, emit=emit)
