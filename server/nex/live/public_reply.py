"""Use the shared planner with the foreground voice model and the device's location channel."""
import asyncio

from ..conversation.access import needs_unlock
from ..conversation.plan import Turn as ConversationTurn
from ..conversation.plan import choose
from ..conversation.reply import run, spoken_text
from ..privacy.requests import queue
from ..voice_stream import SpeechChunks


async def public_reply(dialogue, voice, history, emit, locate=None):
    turn = ConversationTurn(voice.id, voice.device, voice.text, voice.language, voice.zone, voice.started,
                            history=history, journal=voice.journal, spoken=True, exclude=voice.id)

    async def generate(messages, tokens):
        return await dialogue.complete(dialogue.local, messages, tokens, temperature=0)

    plan = await choose(turn, generate)
    if needs_unlock(turn, plan):
        result = await asyncio.to_thread(queue, dialogue.memory.privacy, turn.id, turn.device, turn.text,
                                         turn.preference, turn.zone, journal=turn.journal, screen=plan.screen,
                                         plan=plan, history=history, speaker=voice.speaker,
                                         asked_at=turn.started.isoformat())
    else:
        if plan.location and locate:
            await locate(voice)
        result = await run(dialogue.memory, turn, plan, generate, coordinates=voice.coordinates)
    chunks = SpeechChunks(emit)
    chunks.add(spoken_text(result), result["language"])
    chunks.flush(result["language"], final=True)
    return result | {"public_voice": True}
