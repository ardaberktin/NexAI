"""Model-directed public Chat and recorded voice with private handoff and replay."""
import asyncio
import json
from dataclasses import asdict, replace
from datetime import datetime
from functools import partial

from ..conversation.access import can_preflight, needs_unlock
from ..conversation.clock import DeviceClock
from ..conversation.goal_context import with_goal_state
from ..conversation.plan import validated
from ..conversation.reply import reply, spoken_text
from ..conversation.sync import execute, generator, make_turn, select
from ..live.weather_location import Coordinates
from ..personal_learning import enqueue_context
from ..store import now
from . import chat_settings
from .chat_recall import recall_chat
from .keys import validate_session
from .requests import queue, request_notice, resolve
from .voice import with_approval_notice


def location_request(turn):
    return reply(turn, "", "weather_location_request")


def handoff(assistant, turn, plan, **options):
    return queue(assistant.store.privacy, turn.id, turn.device, turn.text, turn.language, turn.zone,
                 journal=turn.journal, screen=plan.screen, plan=plan, history=turn.history, assistant=assistant,
                 exclude=turn.exclude, device_clock=turn.device_clock, recalled_chat=turn.recalled_chat,
                 attachments=turn.attachments, agent_loop=turn.agent_loop, **options)


def answer(assistant, state, identifier, device, text, preference, zone, *, journal=None, force=False, spoken=False,
           asked_at=None, speaker="unverified", emit=None, lookup_only=False, exclude=None, history=(),
           ask_location=False, location=None):
    turn = make_turn(identifier, device, text, preference, zone, asked_at=asked_at, history=history,
                     journal=journal, spoken=spoken, exclude=exclude)
    plan = select(assistant, turn)
    if plan.error == "model_unavailable":
        if lookup_only:
            return None
        status = reply(turn, "", plan.error)
        return status | {"public_voice": True} if spoken else status
    private = needs_unlock(turn, plan, force)
    if lookup_only and (private or not can_preflight(plan)):
        return None
    if private:
        result = handoff(assistant, turn, plan, asked_at=asked_at, speaker=speaker, spoken=spoken)
        if spoken:
            result = asyncio.run(with_approval_notice(result, generator(assistant, turn)))
    elif ask_location and plan.location and location is None:
        return location_request(turn)
    else:
        coordinates = Coordinates.for_turn(identifier, location) if location is not None and plan.location else None
        result = execute(assistant, turn, plan, coordinates=coordinates)
    if emit and (speech := spoken_text(result)):
        emit(speech, result["language"])
    return result | {"public_voice": True} if spoken else result


def previous_private(store, turn, session, location, ask_location, lookup_only):
    state = store.privacy
    with state.transaction() as db:
        if session:
            validate_session(db, turn.device, session)
        row = db.execute("SELECT r.*,g.goal_id FROM requests r LEFT JOIN request_goals g ON r.id=g.request_id "
                         "WHERE r.id=?", (turn.id,)).fetchone()
        if row is None and db.execute("SELECT 1 FROM private_chat_ids WHERE id=?", (turn.id,)).fetchone():
            raise ValueError("This private exchange has expired or was dismissed.")
    if row is None:
        return None
    data = json.loads(row["payload"])
    if (row["device"], data["text"], data.get("asked_at")) != (turn.device, turn.text, None):
        raise ValueError("This question ID belongs to another exchange.")
    if data.get("journal") != (asdict(turn.journal) if turn.journal else None):
        raise ValueError("This question ID belongs to a different Daybook selection.")
    if lookup_only or not session or row["state"] != "pending":
        notice = request_notice(data)
        return notice | {"goal_id": row["goal_id"]} if session and row["goal_id"] else notice
    if data.get("needs_location") and ask_location and location is None:
        return location_request(turn)
    coordinates = Coordinates.for_turn(turn.id, location) if location is not None and data.get("needs_location") else None
    return coordinates, data


def cached_public(store, turn):
    with store.connect() as db:
        row = db.execute("SELECT * FROM public_chat WHERE id=?", (turn.id,)).fetchone()
        if row:
            if (row["device"], row["question"]) != (turn.device, turn.text):
                raise ValueError("This question ID belongs to another exchange.")
            return json.loads(row["result"]) if row["result"] else None
        if db.execute("SELECT 1 FROM memory WHERE id IN (?,?)", ("q-" + turn.id, "a-" + turn.id)).fetchone():
            raise ValueError("This question ID belongs to an archived exchange.")
    return None


def save_public(store, turn, result, *, pending=False, learn=False):
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("INSERT INTO public_chat VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET result=excluded.result",
                   (turn.id, turn.device, turn.text, json.dumps(result)))
        store.add_memory(db, "q-" + turn.id, now(), "question", turn.device, turn.text)
        if not pending:
            store.add_memory(db, "a-" + turn.id, now(), "answer", turn.device, result["text"],
                             result["sources"], result["engine"])
            if learn:
                enqueue_context(db, "q-" + turn.id, "dialogue", turn.text, now())


def plan_chat(assistant, turn, saved, session, *, force, defaults, lookup_only):
    store = assistant.store
    history = turn.history
    recall = partial(recall_chat, store, turn, session) if session and not lookup_only else None
    if saved:
        turn.started = datetime.fromisoformat(saved["started"]) if saved.get("started") else turn.started
        turn.resolved_language = saved.get("language")
        turn.device_clock = DeviceClock.read(saved.get("device_clock"))
        turn.recalled_chat = saved.get("recalled_chat")
        plan = replace(validated(saved["pending_tools"]), clarify=bool(saved.get("clarify")))
    else:
        turn.history = with_goal_state(store, history)
        plan = select(assistant, turn, recall=recall)
    private = not saved and (needs_unlock(turn, plan, force) or chat_settings.prefers_private(defaults, plan))
    # A private follow-up's context must never enter a public answer or public search terms.
    if history and history[-1].get("private") and not private:
        turn.history = ()
        if not saved:
            turn.recalled_chat = None
            turn.resolved_language = None
            plan = select(assistant, turn, recall=recall)
            private = needs_unlock(turn, plan, force) or chat_settings.prefers_private(defaults, plan)
    return plan, private


def chat(assistant, identifier, device, text, preference, zone, *, journal=None, force=False, lookup_only=False,
         followups=None, session=None, ask_location=False, location=None, device_clock=None,
         standard=False, private_reply=False, attachments=(), agent_loop=False):
    store = assistant.store
    history = followups.previous(device, session) if followups else ()
    clock = DeviceClock.read(device_clock)
    turn = make_turn(identifier, device, text, preference, zone, history=history, journal=journal,
                     device_clock=clock, attachments=tuple(attachments or ()), agent_loop=bool(agent_loop))
    existing = previous_private(store, turn, session, location, ask_location, lookup_only)
    if existing is not None:
        if isinstance(existing, tuple):
            coordinates, data = existing
            goal = resolve(store.privacy, device, session, identifier, assistant, coordinates)
            if followups:
                followups.remember_private(store.privacy, device, session, identifier, text, data.get("tools", []))
            notice = request_notice(data)
            return notice | {"goal_id": goal} if goal else notice
        return None if lookup_only else existing
    saved = cached_public(store, turn)
    if saved and (force or journal):
        raise ValueError("This question ID already belongs to a public exchange.")
    if saved and "pending_tools" not in saved:
        return saved if not lookup_only or saved["engine"] in {"web_lookup", "python_sandbox", "calculation"} else None
    if lookup_only and saved and saved.get("recalled_chat") is not None:
        return None
    preferences = chat_settings.settings(store.privacy)
    if force and not preferences["enabled"] and not (private_reply or journal):
        return None if lookup_only else reply(turn, "", "private_chat_disabled")
    defaults = preferences if preferences["enabled"] and not standard else {}
    plan, private = plan_chat(assistant, turn, saved, session, force=force, defaults=defaults, lookup_only=lookup_only)
    if plan.error == "model_unavailable":
        return None if lookup_only else reply(turn, "", plan.error)
    if lookup_only and (private or not can_preflight(plan)):
        return None
    coordinates = Coordinates.for_turn(identifier, location) if plan.location and location is not None else None
    ask = ask_location and plan.location and location is None and journal is None
    if private:
        result = handoff(assistant, turn, plan, session=session, coordinates=coordinates, defer=bool(session and ask),
                         typed_chat=True)
        if ask and session:
            return location_request(turn)
        if followups:
            followups.remember_private(store.privacy, device, session, identifier, text, plan.tools)
        return result
    if not saved and not lookup_only:
        pending = {"pending_tools": plan.tools, "started": turn.started.isoformat(), "language": turn.language}
        if turn.device_clock is not None:
            pending["device_clock"] = asdict(turn.device_clock)
        if turn.recalled_chat is not None:
            pending["recalled_chat"] = turn.recalled_chat
        if plan.clarify:
            pending["clarify"] = True
        save_public(store, turn, pending, pending=True)
    if ask:
        return location_request(turn)
    result = execute(assistant, turn, plan, coordinates=coordinates)
    if not result["text"]:
        return result
    save_public(store, turn, result, learn=bool(session and not lookup_only))
    if followups:
        followups.remember(device, session, identifier, text, result)
    return result
