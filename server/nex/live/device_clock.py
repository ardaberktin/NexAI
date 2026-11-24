"""Match a device clock reply to the live turn that requested it."""

import asyncio

from ..conversation.clock import DeviceClock


class DeviceClockTurn:
    def __init__(self, identifier):
        self.id = identifier
        self.reply = asyncio.get_running_loop().create_future()

    async def wait(self):
        try:
            return await asyncio.wait_for(self.reply, 2)
        except TimeoutError:
            return None

    def receive(self, message):
        if message.get("id") == self.id and not self.reply.done():
            self.reply.set_result(DeviceClock.read(message.get("device_clock")))
