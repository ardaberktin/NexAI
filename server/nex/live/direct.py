"""Compatibility entry point for callers of the original live tool router."""

from .public_reply import public_reply


async def direct_reply(dialogue, turn, history, emit, coordinates=None, location_requested=False):
    turn.coordinates = coordinates
    turn.location_requested = location_requested
    return await public_reply(dialogue, turn, history, emit)
