import tempfile
import unittest
from datetime import UTC, datetime
from unittest.mock import Mock

from nex.goals import Goals
from nex.store import Store
from nex.workspace_content import chat_history

LEGACY_GOALS = """CREATE TABLE goals (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, instructions TEXT NOT NULL, kind TEXT NOT NULL,
    topic TEXT NOT NULL, zone TEXT NOT NULL, interval_minutes INTEGER NOT NULL, reminder_at TEXT,
    status TEXT NOT NULL, created TEXT NOT NULL, updated TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1, next_run TEXT, last_checked TEXT, error TEXT, latest TEXT,
    requested INTEGER NOT NULL DEFAULT 0, briefing_time TEXT, prompt TEXT, scheduled_at TEXT
)"""


class GoalMigrationTests(unittest.TestCase):
    def test_old_rows_become_prompts_and_keep_state_schedule_and_history(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        store = Store(folder.name, 0, 2**20)
        created = "2026-11-22T06:00:00.000+00:00"
        due = "2026-11-22T08:30:00.000+00:00"
        legacy = [
            {"id": "email", "title": "My alerts", "kind": "email", "topic": "lab updates", "interval": 60},
            {"id": "day", "title": "Morning report", "kind": "briefing", "interval": 1440,
             "status": "active", "next_run": due, "clock": "08:30"},
            {"id": "calendar", "title": "Calendar", "kind": "calendar", "topic": "lab", "interval": 15},
            {"id": "note", "title": "Reminder", "kind": "reminder", "notes": "Bring notes",
             "status": "active", "next_run": due, "reminder": due},
            {"id": "prompt", "title": "Saved", "kind": "goal", "interval": 60, "status": "completed",
             "prompt": "Keep this", "scheduled": created},
        ]
        defaults = {"notes": "", "topic": "", "interval": 0, "created": created, "status": "paused",
                    "reminder": None, "next_run": None, "clock": None, "prompt": None, "scheduled": None}
        with store.connect() as db:
            db.execute("DROP TABLE goals")
            db.execute(LEGACY_GOALS)
            db.executemany(
                "INSERT INTO goals(id,title,instructions,kind,topic,zone,interval_minutes,reminder_at,status,"
                "created,updated,revision,next_run,briefing_time,prompt,scheduled_at) "
                "VALUES(:id,:title,:notes,:kind,:topic,'UTC',:interval,:reminder,:status,"
                ":created,:created,7,:next_run,:clock,:prompt,:scheduled)",
                [defaults | row for row in legacy],
            )
            db.execute("DROP TABLE goal_activity")
            db.execute("""CREATE TABLE goal_activity (
                id TEXT PRIMARY KEY, goal_id TEXT NOT NULL, created TEXT NOT NULL,
                title TEXT NOT NULL, detail TEXT NOT NULL, artifact INTEGER NOT NULL DEFAULT 0
            )""")
            db.execute("INSERT INTO goal_activity(rowid,id,goal_id,created,title,detail,artifact) "
                       "VALUES(42,'original','email',?,'Matching email','Old update',1)", (created,))
            db.execute("INSERT INTO goal_seen VALUES('email','seen-message',?)", (created,))
            db.execute("INSERT INTO goal_mail_review VALUES('email','reviewed-message',?)", (created,))
            db.execute("ALTER TABLE goal_push_delivery ADD COLUMN kind TEXT")
            db.execute("INSERT INTO goal_push_delivery VALUES('original','token',100,200,2,'email')")

        reopened = Store(folder.name, 0, 2**20)
        goals = Goals(reopened)
        migrated = {item["id"]: item for item in goals.overview()}
        self.assertIn('exact phrase "lab updates"', migrated["email"]["prompt"])
        self.assertEqual((migrated["email"]["status"], migrated["email"]["revision"]), ("paused", 7))
        self.assertEqual((migrated["day"]["scheduled_at"], migrated["day"]["next_run"]), (due, due))
        self.assertEqual(migrated["note"]["prompt"], "Remind me about this: Bring notes")
        self.assertIn("newly found Google Calendar events that match lab", migrated["calendar"]["prompt"])
        self.assertEqual(migrated["prompt"]["prompt"], "Keep this")
        self.assertEqual(chat_history(reopened)[0]["id"], "original")
        with reopened.connect() as db:
            names = {column["name"] for column in db.execute("PRAGMA table_info(goals)")}
            self.assertTrue({"kind", "topic", "instructions", "reminder_at", "briefing_time"}.isdisjoint(names))
            self.assertEqual(db.execute("SELECT sequence FROM goal_activity WHERE id='original'").fetchone()[0], 42)
            self.assertEqual(db.execute("SELECT fingerprint FROM goal_seen").fetchone()[0], "seen-message")
            self.assertEqual(db.execute("SELECT fingerprint FROM goal_mail_review").fetchone()[0], "reviewed-message")
            queued = dict(db.execute("SELECT * FROM goal_push_delivery").fetchone())
            self.assertEqual(queued, {"id": "original", "token": "token", "next_try": 100, "expires": 200, "attempts": 2})

        generate = Mock(side_effect=['{"tools":[]}', "Bring your notes.", '{"tools":[]}', "Good morning."])
        Goals(reopened, generate=generate).run(datetime.fromisoformat(due).astimezone(UTC))
        self.assertEqual(generate.call_count, 4)
        self.assertEqual(goals.detail("note")["artifact_count"], 1)
        self.assertEqual(goals.detail("day")["artifact_count"], 1)
        self.assertEqual(Goals(Store(folder.name, 0, 2**20)).detail("email")["artifact_count"], 1)
