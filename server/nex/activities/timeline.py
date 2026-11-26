"""Read recordings in capture order and fold consecutive observations into visible periods."""

import json
from datetime import datetime, timedelta

from ..recall_dates import time_key
from ..workspace_features import enabled
from .recognition import TITLES
from .schema import interval

BASIS_NAMES = {"sound": "Room audio", "speech": "Recorded speech", "screen": "Shared Mac screen",
               "device": "Device playback", "voice": "Nex voice session"}


def confidence(category, basis):
    if category == "unknown":
        return "Unclear"
    return {"sound": "Possible", "speech": "Likely", "screen": "Visible on Mac",
            "device": "Likely", "voice": "Observed"}[basis]


def public_clip(row, start, end):
    category, basis = row["category"], row["basis"]
    sounds = json.loads(row["sounds"] or "[]")
    custom = row["title"] if "title" in row.keys() else None
    title = custom or (f"{sounds[0]} detected" if category == "unknown" and sounds else TITLES.get(category, "Activity"))
    return {"id": row["id"], "started": start, "ended": end, "device": row["device"],
            "category": category, "title": title, "basis": BASIS_NAMES[basis],
            "confidence": confidence(category, basis), "detail": row["detail"],
            "sounds": sounds}


def clipped_rows(store, bounds, exclude=None, as_of=None):
    lower, upper = map(datetime.fromisoformat, bounds)
    # The upload API limits recordings to 65 seconds. Include a clip crossing the local midnight.
    with store.connect() as db:
        if not enabled(db, "activities"):
            return
        rows = db.execute(
            """SELECT r.id,r.started,r.duration,r.device,a.category,a.basis,a.detail,a.sounds,a.title
               FROM recordings r JOIN activity_clips a ON a.recording_id=r.id
               WHERE r.started>=? AND r.started<? AND (? IS NULL OR r.id<>?) ORDER BY r.started,r.id""",
            (time_key(lower - timedelta(seconds=65)), time_key(upper), exclude, exclude),
        )
        for row in rows:
            start, end = interval(row)
            if end > lower and (as_of is None or end <= as_of):
                yield row, max(start, lower), min(end, upper)


def timeline(store, bounds, *, source=None, exclude=None, allowed=None, as_of=None):
    periods, last, selected = [], {}, []
    pending = 0
    for row, start, end in clipped_rows(store, bounds, exclude, as_of):
        if allowed is not None and row["id"] not in allowed:
            continue
        device = row["device"]
        if row["category"] is None:
            pending += 1
            last.pop(device, None)
            continue
        clip = public_clip(row, time_key(start), time_key(end))
        previous = last.get(device)
        if (previous and previous["category"] == clip["category"] and previous["basis"] == clip["basis"]
                and previous["title"] == clip["title"]
                and (previous["detail"] == clip["detail"] or (row["basis"] == "sound" and clip["category"] != "unknown"))
                and start <= datetime.fromisoformat(previous["ended"]) + timedelta(seconds=1)):
            period = previous
            period["ended"] = max(period["ended"], clip["ended"])
            period["count"] += 1
        else:
            period = clip | {"count": 1}
            periods.append(period)
            last[device] = period
        if source is not None:
            period.setdefault("observations", []).append(clip)
            if row["id"] == source:
                selected = period["observations"]
    if source is not None:
        return {"items": selected}
    return {"items": periods, "pending": pending}


def activity_passage(period, zone):
    start, end = (datetime.fromisoformat(period[key]).astimezone(zone) for key in ("started", "ended"))
    detail = period["detail"]
    return {"id": period["id"], "started": period["started"], "kind": "activity",
            "text": (f"{start:%Y-%m-%d %H:%M}–{end:%H:%M %Z} · {period['title']} "
                     f"({period['confidence']} · {period['basis']}). {detail}")}
