"""Use the shared planner with the foreground voice model and ask the device for context."""
import asyncio

from ..conversation.access import needs_unlock
from ..conversation.plan import Turn as ConversationTurn
from ..conversation.plan import choose
from ..conversation.reply import run, spoken_text
from ..privacy.requests import queue
from ..voice_stream import SpeechChunks


async def public_reply(dialogue, voice, history, emit, locate=None, *, read_clock=None):
    turn = ConversationTurn(voice.id, voice.device, voice.text, voice.language, voice.zone, voice.started,
                            history=history, journal=voice.journal, spoken=True, exclude=voice.id)
    if read_clock is not None:
        turn.device_clock = await read_clock(turn.id)

    async def generate(messages, tokens):
        return await dialogue.complete(dialogue.local, messages, tokens, temperature=0)

    plan = await choose(turn, generate)
    if needs_unlock(turn, plan) and plan.error != "model_unavailable":
        result = await asyncio.to_thread(queue, dialogue.memory.privacy, turn.id, turn.device, turn.text,
                                         turn.language, turn.zone, journal=turn.journal, screen=plan.screen,
                                         plan=plan, history=history, speaker=voice.speaker,
                                         asked_at=turn.started.isoformat(), device_clock=turn.device_clock)
    else:
        if plan.location and locate:
            await locate(voice)
        result = await run(dialogue.memory, turn, plan, generate, coordinates=voice.coordinates)
    chunks = SpeechChunks(emit)
    chunks.add(spoken_text(result), result["language"])
    chunks.flush(result["language"], final=True)
    return result | {"public_voice": True}
