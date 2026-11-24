"""Exchange just the local Mac answer for an individual live voice turn."""

import asyncio

MAX_REPLY = 1200


class ScreenTurn:
    def __init__(self, turn_id):
        self.id = turn_id
        self.reply = asyncio.get_running_loop().create_future()

    async def wait(self, seconds=55):
        return await asyncio.wait_for(self.reply, seconds)

    def receive(self, payload):
        if not isinstance(payload, dict) or payload.get("kind") != "screen_result":
            raise ValueError("Unknown voice control message")
        if payload.get("id") != self.id or self.reply.done():
            return
        text = payload.get("text")
        if text is not None:
            if not isinstance(text, str) or len(text) > MAX_REPLY:
                raise ValueError("Invalid screen reply")
            text = text.strip()
        self.reply.set_result(text or None)
