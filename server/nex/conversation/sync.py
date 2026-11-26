"""Adapter for archive HTTP and recording workers, which run one request per thread."""
import asyncio
from datetime import UTC, datetime

from .plan import Turn, choose
from .reply import run


def make_turn(identifier, device, text, preference, zone, *, asked_at=None, **kwargs):
    anchor = datetime.fromisoformat(asked_at) if isinstance(asked_at, str) else asked_at
    return Turn(identifier, device, text, preference, zone, anchor or datetime.now(UTC), **kwargs)


def generator(assistant, turn):
    config = assistant.settings.read() if assistant.settings else {}

    async def generate(messages, tokens):
        value = await asyncio.to_thread(assistant.generate_conversation, messages, config, turn.language,
                                        tokens, None, use_warm=True, temperature=0)
        return value if isinstance(value, str) else None
    return generate


def select(assistant, turn):
    return asyncio.run(choose(turn, generator(assistant, turn)))


def execute(assistant, turn, plan, **options):
    return asyncio.run(run(assistant.store, turn, plan, generator(assistant, turn), **options))
