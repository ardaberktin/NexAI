"""Private questions and answers kept outside searchable memory."""

import json
from dataclasses import asdict
from datetime import UTC, datetime
from time import time

from ..clock_alerts import ClockAlerts
from ..goal_chat_receipts import resolve_receipts
from ..live.weather_location import Coordinates
from ..pdf import artifacts as pdf_artifacts
from .keys import consume, validate_session
from .planned import approved_reply
from .state import AccessDenied, approval_subject

PENDING_SECONDS = 600
RETENTION_SECONDS = 7 * 86400


def prune(db, now):
    db.execute("DELETE FROM requests WHERE created<? OR (state='pending' AND created<?)",
               (now - RETENTION_SECONDS, now - PENDING_SECONDS))
    db.execute("UPDATE requests SET state='failed' WHERE state IN ('working','screen') AND updated<?", (now - 300,))


def queue(state, identifier, device, text, language, zone, *, journal=None, screen=False,
          speaker="unverified", asked_at=None, exclude=None, mail=None, session=None, coordinates=None,
          assistant=None, action=None, plan=None, history=(), defer=False, device_clock=None,
          recalled_chat=None, spoken=False):
    data = {"text": text, "language": language, "zone": zone, "screen": screen,
            "needs_location": bool(plan and plan.location and not journal), "exclude": exclude,
            "asked_at": asked_at, "journal": asdict(journal) if journal else None}
    if plan is not None:
        data.update(tools=list(plan.tools), plan_error=plan.error, clarify=plan.clarify, history=list(history))
    if recalled_chat is not None:
        data["recalled_chat"] = recalled_chat
    if mail:
        data["mail"] = asdict(mail)
    if action:
        data["action"] = action
    if device_clock is not None:
        data["device_clock"] = asdict(device_clock)
    payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
    now = time()
    work = None
    with state.transaction(write=True) as db:
        if session:
            validate_session(db, device, session)
        prune(db, now)
        row = db.execute("SELECT * FROM requests WHERE id=?", (identifier,)).fetchone()
        if row and (row["device"] != device or row["payload"] != payload):
            raise ValueError("Choose a new ID for a different private question.")
        if not row:
            pending = db.execute("SELECT count(*) FROM requests WHERE device=? AND state='pending'", (device,)).fetchone()
            if not session and pending[0] >= 24:
                return request_notice(data, status="private_queue_full")
            db.execute("INSERT INTO requests VALUES(?,?,?,?,?,'pending',?,NULL)",
                       (identifier, device, payload, speaker, now, now))
        if session and not defer and (not row or row["state"] == "pending"):
            work = begin(db, identifier, device, row["created"] if row else now, data)
    notice = request_notice(data)
    if work:
        goal = complete(state, identifier, work, assistant, coordinates)
        if goal:
            notice["goal_id"] = goal
    if spoken and not session:
        notice["approval_pending"] = row is None or row["state"] == "pending"
    return notice


def request_notice(data, status="private_request"):
    return {"text": "", "language": data["language"], "sources": [], "engine": status}


def inbox(state, store=None):
    with state.transaction(write=True) as db:
        prune(db, time())
        rows = db.execute("SELECT requests.*,citations.sources,request_goals.goal_id FROM requests "
                          "LEFT JOIN citations ON citations.request_id=requests.id "
                          "LEFT JOIN request_goals ON request_goals.request_id=requests.id "
                          "ORDER BY created DESC LIMIT 100").fetchall()
        items = [inbox_item(row) for row in rows]
        resolve_receipts(db, state.root, items)
    if store is not None:
        attach_pdfs(store, items)
        alerts = ClockAlerts(store).for_requests([item["id"] for item in items if item["state"] != "pending"])
        for item in items:
            if alert := alerts.get(item["id"]):
                item["clock_alert"] = alert
    return {"items": items}


def attach_pdfs(store, items):
    candidates = {pdf_artifacts.request_id(item["device"], item["id"]): item for item in items
                  if item["state"] in {"ready", "failed"}}
    if candidates:
        with store.connect() as db:
            attachments = pdf_artifacts.by_ids(db, candidates)
        for identifier, attachment in attachments.items():
            candidates[identifier]["pdf_attachment"] = attachment


def question_time(data, created):
    if isinstance(value := data.get("asked_at"), str):
        try:
            asked = datetime.fromisoformat(value)
            if asked.tzinfo is not None:
                return asked.astimezone(UTC)
        except ValueError:
            pass
    return datetime.fromtimestamp(created, UTC)


def inbox_item(row):
    data = json.loads(row["payload"])
    return {"id": row["id"], "device": row["device"], "question": data["text"], "speaker": row["speaker"],
            "state": row["state"], "answer": row["answer"], "subject": approval_subject(row),
            "asked_at": question_time(data, row["created"]).isoformat(timespec="milliseconds"),
            "updated_at": datetime.fromtimestamp(row["updated"], UTC).isoformat(timespec="milliseconds"),
            "needs_location": data.get("needs_location", False),
            "sources": json.loads(row["sources"]) if row["sources"] else [], "goal_id": row["goal_id"]}


def approve(state, device, identifier, proof, assistant):
    if device != "iphone":
        raise AccessDenied("Face ID on the paired iPhone is required.")
    with state.transaction(write=True) as db:
        row = db.execute("SELECT * FROM requests WHERE id=? AND state='pending' AND created>?",
                         (identifier, time() - PENDING_SECONDS)).fetchone()
        if not row:
            raise AccessDenied("This approval has expired or was already used.")
        consume(db, device, "approve", approval_subject(row), proof)
        data = json.loads(row["payload"])
        coordinates = Coordinates.from_reply(proof) if data.get("needs_location") else None
        work = begin(db, identifier, row["device"], row["created"], data)
    complete(state, identifier, work, assistant, coordinates)


def resolve(state, device, token, identifier, assistant, coordinates):
    if not isinstance(identifier, str) or len(identifier) > 64:
        raise ValueError("Choose a private question to show.")
    with state.transaction(write=True) as db:
        validate_session(db, device, token)
        prune(db, time())
        row = db.execute("SELECT * FROM requests WHERE id=?", (identifier,)).fetchone()
        if not row:
            raise AccessDenied("This private question has expired.")
        if row["state"] != "pending":
            return
        work = begin(db, identifier, row["device"], row["created"], json.loads(row["payload"]))
    return complete(state, identifier, work, assistant, coordinates)


def begin(db, identifier, device, created, data):
    status = "screen" if data["screen"] else "working"
    db.execute("UPDATE requests SET state=?,updated=? WHERE id=?", (status, time(), identifier))
    return device, created, data


def complete(state, identifier, work, assistant, coordinates):
    device, created, data = work
    if data["screen"]:
        return
    try:
        result = approved_reply(assistant, identifier, device, data, question_time(data, created), coordinates)
        goal = result.get("goal_id")
        if not result["text"]:
            fail(state, identifier, goal=goal)
            return goal
        finish(state, identifier, result["text"], "working", result.get("sources", []), goal=goal,
               recall=result.get("recall_cursor"), goal_choices=result.get("goal_choices"))
        return goal
    except Exception:
        fail(state, identifier)
        raise


def fail(state, identifier, *, goal=None):
    with state.transaction(write=True) as db:
        updated = db.execute("UPDATE requests SET state='failed' WHERE id=? AND state='working'", (identifier,))
        if updated.rowcount and goal:
            db.execute("INSERT INTO request_goals VALUES(?,?)", (identifier, goal))


def finish(state, identifier, answer, previous, sources=(), *, goal=None, recall=None, goal_choices=None):
    if not isinstance(answer, str) or not 0 < len(answer) <= 64_000:
        raise ValueError("Invalid private answer.")
    with state.transaction(write=True) as db:
        updated = db.execute("UPDATE requests SET answer=?,state='ready',updated=? WHERE id=? AND state=?",
                             (answer, time(), identifier, previous))
        if not updated.rowcount:
            raise AccessDenied("This request is no longer awaiting a result.")
        if sources:
            db.execute("INSERT INTO citations VALUES(?,?)", (identifier, json.dumps(sources)))
        if goal:
            db.execute("INSERT INTO request_goals VALUES(?,?)", (identifier, goal))
        if recall:
            db.execute("INSERT INTO request_recall VALUES(?,?)", (identifier, json.dumps(recall)))
        if goal_choices:
            db.execute("INSERT INTO request_goal_choices VALUES(?,?)", (identifier, json.dumps(goal_choices)))
