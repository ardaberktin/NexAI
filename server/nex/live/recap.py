"""Prepare a spoken daily recap from its cited source and verify cross-language wording."""

import asyncio

import httpx

from ..daybook import Daybook
from ..recap_translation import (
    grounded_translation,
    translation_candidate,
    translation_messages,
)
from ..response_language import reply_language


async def spoken_recap(dialogue, turn, context):
    source = await asyncio.to_thread(Daybook(dialogue.memory).ready_recap, context, turn.id)
    return await spoken_translation(dialogue, source, reply_language(turn.text, turn.language))


async def spoken_translation(dialogue, source, language):
    if not source or source["language"] == language:
        return source

    async def translate(result, target):
        return await dialogue.complete(dialogue.local, translation_messages(result, target), 180, temperature=0)

    try:
        async with asyncio.timeout(9):
            text = await translate(source, language)
            if candidate := translation_candidate(source, language, text):
                return grounded_translation(source, candidate, await translate(candidate, source["language"]))
    except (TimeoutError, httpx.HTTPError, ValueError, KeyError, IndexError):
        pass
    return source
