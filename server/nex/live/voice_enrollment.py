"""Enroll directly from short-lived in-memory samples sent by an unlocked native app."""

import asyncio
import base64
import json

from fastapi import HTTPException, Request

from ..privacy.keys import consume, require_session
from ..privacy.state import AccessDenied
from .access import workspace_for
from .voice_profiles import ENROLLMENT_BYTES, enrollment_subject

ENCODED_AUDIO_BYTES = 4 * ((ENROLLMENT_BYTES + 2) // 3)
# Apple's JSONEncoder can write every base64 slash as \\/. Allow that wire
# representation and keep the decoded audio at exactly three samples.
MAX_BODY_BYTES = 2 * ENCODED_AUDIO_BYTES + 4_000
SAMPLE_ERROR = "Record three four-second voice samples."


def install_voice_enrollment(app, authenticate):
    @app.api_route("/v2/voice/profile", methods=["GET", "POST"])
    @app.api_route("/v1/voice/profile", methods=["GET", "POST"])
    async def profile(request: Request):
        client = authenticate(request)
        if not client:
            raise HTTPException(401)
        voices = workspace_for(request).voices
        device = client.role
        try:
            require_session(voices.state, client, request.headers.get("X-Nex-Workspace", ""))
            if request.method == "GET":
                with voices.state.transaction() as db:
                    return profile_status(db, device, available=voices.room is not None)
            body = await enrollment_body(request)
            if body.get("action") not in ("delete", "enroll"):
                raise ValueError("Choose a valid voice profile action.")
            removing = body.get("action") == "delete"
            raw = b"" if removing else enrollment_audio(body.get("audio"))
            subject = "delete" if removing else enrollment_subject(raw)
            with voices.state.transaction(write=True) as db:
                consume(db, client, "voice", subject, body)
                db.execute("INSERT INTO voice_changes VALUES(?,?) ON CONFLICT(device) DO UPDATE SET nonce=excluded.nonce",
                           (device, body["nonce"]))
                if removing:
                    db.execute("DELETE FROM voices WHERE device=?", (device,))
                    return profile_status(db, device, available=voices.room is not None)
            if voices.room is None:
                raise HTTPException(503, "Install the Excalibur room voice models.")
            template = await asyncio.to_thread(voices.enrollment, raw)
            with voices.state.transaction(write=True) as db:
                if db.execute("SELECT nonce FROM voice_changes WHERE device=?", (device,)).fetchone()[0] != body["nonce"]:
                    raise AccessDenied("A newer voice profile change superseded this enrollment.")
                db.execute("INSERT INTO voices VALUES(?,?) ON CONFLICT(device) DO UPDATE SET template=excluded.template",
                           (device, template))
                return profile_status(db, device, available=True)
        except AccessDenied as error:
            raise HTTPException(403, str(error)) from error
        except (KeyError, ValueError, TypeError) as error:
            raise HTTPException(400, str(error)) from error


def profile_status(db, device, *, available):
    enrolled, shared_enrolled = db.execute(
        "SELECT EXISTS(SELECT 1 FROM voices WHERE device=?), EXISTS(SELECT 1 FROM voices)", (device,)
    ).fetchone()
    return {"enrolled": bool(enrolled), "shared_enrolled": bool(shared_enrolled), "available": available}


async def enrollment_body(request):
    length = request.headers.get("content-length")
    size = int(length) if length is not None else None
    if size is not None and size > MAX_BODY_BYTES:
        raise HTTPException(413, SAMPLE_ERROR)
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > MAX_BODY_BYTES:
            raise HTTPException(413, SAMPLE_ERROR)
        raw.extend(chunk)
    if size is not None and size != len(raw):
        raise ValueError("The voice profile upload was incomplete. Try saving again.")
    body = json.loads(raw)
    if not isinstance(body, dict):
        raise TypeError("Invalid voice profile request.")
    return body


def enrollment_audio(encoded):
    if not isinstance(encoded, str) or len(encoded) != ENCODED_AUDIO_BYTES:
        raise ValueError(SAMPLE_ERROR)
    raw = base64.b64decode(encoded, validate=True)
    if len(raw) != ENROLLMENT_BYTES:
        raise ValueError(SAMPLE_ERROR)
    return raw
