"""Validate approximate device coordinates and match them to one weather turn."""

import asyncio
from dataclasses import dataclass


@dataclass(frozen=True)
class Coordinates:
    latitude: float
    longitude: float

    @classmethod
    def for_turn(cls, identifier, payload):
        if not isinstance(payload, dict) or payload.get("id") != identifier:
            raise ValueError("Weather location must belong to this question.")
        return cls.from_reply(payload)

    @classmethod
    def from_reply(cls, payload):
        latitude, longitude = payload.get("latitude"), payload.get("longitude")
        if latitude is None and longitude is None:
            return None
        if (type(latitude) not in (int, float) or not -90 <= latitude <= 90
                or type(longitude) not in (int, float) or not -180 <= longitude <= 180):
            raise ValueError("Invalid weather coordinates")
        return cls(round(latitude, 2), round(longitude, 2))


class WeatherLocationTurn:
    def __init__(self, turn_id):
        self.id = turn_id
        self.reply = asyncio.get_running_loop().create_future()

    async def wait(self, seconds=25):
        try:
            return await asyncio.wait_for(self.reply, seconds)
        except TimeoutError:
            return None

    def receive(self, payload):
        if payload.get("id") == self.id and not self.reply.done():
            self.reply.set_result(Coordinates.from_reply(payload))
