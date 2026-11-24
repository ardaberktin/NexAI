"""Device clock samples with the UTC offset observed by the client."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ..recall_dates import time_zone


@dataclass(frozen=True)
class DeviceClock:
    unix_seconds: int
    utc_offset_seconds: int
    time_zone: str

    @classmethod
    def read(cls, payload):
        if not isinstance(payload, dict):
            return None
        seconds, offset = payload.get("unix_seconds"), payload.get("utc_offset_seconds")
        zone = payload.get("time_zone")
        if type(seconds) is not int or type(offset) is not int or abs(offset) > 18 * 3600:
            return None
        clock = cls(seconds, offset, zone)
        try:
            time_zone(zone)
            clock.local_time()
        except (ValueError, OverflowError, OSError):
            return None
        return clock

    def local_time(self):
        return datetime.fromtimestamp(self.unix_seconds, timezone(timedelta(seconds=self.utc_offset_seconds)))
