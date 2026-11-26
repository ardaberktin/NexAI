"""Owner-only workspace routes for the paired native apps."""

from ..live.weather_location import Coordinates
from .keys import challenge, require_session, unlock
from .requests import approve, finish, inbox, resolve
from .state import AccessDenied, digest


def route(api, method, path, device, problem):
    state = api.server.privacy
    token = api.headers.get("X-Nex-Workspace", "")
    if method == "GET" and path == "version":
        return {"version": 1}
    if method == "POST" and path == "challenge":
        data = api.object(1500)
        if data.get("purpose") != "unlock":
            require_session(state, device, token)
        return challenge(state, device, data.get("purpose"), data.get("subject", ""))
    if method == "POST" and path == "unlock":
        return unlock(state, device, api.object(3000))
    require_session(state, device, token)
    if method == "GET" and path == "inbox":
        return inbox(state)
    if method == "POST" and path in {"approve", "resolve"}:
        data = api.object(2500)
        if not api.app.chat_slot.acquire(blocking=False):
            raise problem(429, "Nex is answering another question. Try again shortly.")
        try:
            if path == "resolve":
                resolve(state, device, token, data.get("id"), api.app.assistant, Coordinates.from_reply(data))
            else:
                approve(state, device, data.get("id"), data, api.app.assistant)
        finally:
            api.app.chat_slot.release()
    elif method == "POST" and path == "screen" and device == "mac":
        data = api.object(300_000)
        finish(state, data.get("id"), data.get("answer"), "screen")
    elif method == "POST" and path == "dismiss":
        data = api.object(500)
        with state.transaction(write=True) as db:
            db.execute("DELETE FROM requests WHERE id=? AND state IN ('pending','ready','failed')", (data.get("id"),))
    elif method == "POST" and path == "lock":
        with state.transaction(write=True) as db:
            db.execute("DELETE FROM sessions WHERE token=? AND device=?", (digest(token), device))
    else:
        raise AccessDenied("This private operation is unavailable.")
    return {"ok": True}
