"""Invalidate only the local Daybook dates affected by new evidence."""

from datetime import UTC, datetime, timedelta

from .recall_dates import time_zone


def schedule_recordings(db, recordings):
    zones = {row[0] for row in db.execute("SELECT DISTINCT zone FROM daybook_reports")}
    anchor = datetime.now(UTC)
    affected = set()
    for row in recordings:
        start = datetime.fromisoformat(row["started"])
        end = max(start, start + timedelta(seconds=row["duration"], microseconds=-1))
        for zone in zones | {row["time_zone"]}:
            tz = time_zone(zone)
            for day in {start.astimezone(tz).date(), end.astimezone(tz).date()}:
                delay = 300 if day >= anchor.astimezone(tz).date() else 15
                due = (anchor + timedelta(seconds=delay)).isoformat(timespec="milliseconds")
                affected.add((day.isoformat(), zone, due))
    db.executemany(
        """INSERT INTO daybook_reports(day,zone,due) VALUES(?,?,?)
           ON CONFLICT(day,zone) DO UPDATE SET version=version+1,
           due=COALESCE(daybook_reports.due,excluded.due)""", sorted(affected),
    )
