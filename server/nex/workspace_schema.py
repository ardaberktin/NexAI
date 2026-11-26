"""Persistent state for Nex's daily feed and ongoing goals."""

GOALS_TABLE = """
CREATE TABLE IF NOT EXISTS goals (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, prompt TEXT NOT NULL,
    zone TEXT NOT NULL, interval_minutes INTEGER NOT NULL, scheduled_at TEXT,
    status TEXT NOT NULL, created TEXT NOT NULL, updated TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1, next_run TEXT, last_checked TEXT, error TEXT, latest TEXT,
    requested INTEGER NOT NULL DEFAULT 0
)"""
GOALS_INDEX = "CREATE INDEX IF NOT EXISTS goals_due ON goals(status, next_run)"

WORKSPACE_SCHEMA = f"{GOALS_TABLE};\n{GOALS_INDEX};\n" + """
CREATE TABLE IF NOT EXISTS watch_recording_state (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1), changed_at REAL NOT NULL, recording INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS feed_settings (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    topics TEXT NOT NULL, instructions TEXT NOT NULL, zone TEXT NOT NULL, hour INTEGER NOT NULL,
    minute INTEGER NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
    requested INTEGER NOT NULL DEFAULT 1, retry_at TEXT, error TEXT, prompt TEXT, deferred_until_day TEXT
);
CREATE TABLE IF NOT EXISTS feed_editions (
    day TEXT PRIMARY KEY, generated TEXT NOT NULL, stories TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS feed_saved (
    id TEXT PRIMARY KEY, story TEXT NOT NULL, saved TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS goal_activity (
    id TEXT PRIMARY KEY, goal_id TEXT NOT NULL, created TEXT NOT NULL,
    title TEXT NOT NULL, detail TEXT NOT NULL, artifact INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS iphone_push_registration (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1), installation TEXT NOT NULL, token TEXT NOT NULL,
    environment TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS goal_push_delivery (
    id TEXT PRIMARY KEY, token TEXT NOT NULL,
    next_try INTEGER NOT NULL, expires INTEGER NOT NULL, attempts INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS goal_push_due ON goal_push_delivery(next_try);
CREATE INDEX IF NOT EXISTS goal_activity_recent ON goal_activity(goal_id, created DESC);
CREATE INDEX IF NOT EXISTS goal_activity_chat_page ON goal_activity(created DESC, id DESC) WHERE artifact=1;
CREATE TABLE IF NOT EXISTS goal_seen (
    goal_id TEXT NOT NULL, fingerprint TEXT NOT NULL, seen TEXT NOT NULL,
    PRIMARY KEY(goal_id, fingerprint)
);
CREATE TABLE IF NOT EXISTS goal_mail_review (
    goal_id TEXT NOT NULL, fingerprint TEXT NOT NULL, reviewed TEXT NOT NULL,
    PRIMARY KEY(goal_id, fingerprint)
);
CREATE TABLE IF NOT EXISTS shared_weather_locations (
    device TEXT PRIMARY KEY, consent TEXT NOT NULL,
    latitude REAL, longitude REAL, observed REAL
);
"""


def migrate_workspace(db):
    db.execute("BEGIN IMMEDIATE")
    feed_columns = {column["name"] for column in db.execute("PRAGMA table_info(feed_settings)")}
    for column in ("prompt", "deferred_until_day"):
        if column not in feed_columns:
            db.execute(f"ALTER TABLE feed_settings ADD COLUMN {column} TEXT")
    goal_columns = {column["name"] for column in db.execute("PRAGMA table_info(goals)")}
    if "kind" in goal_columns:
        from .goal_migration import migrate_goals

        migrate_goals(db)
    delivery_columns = {column["name"] for column in db.execute("PRAGMA table_info(goal_push_delivery)")}
    if "kind" in delivery_columns:
        db.execute("ALTER TABLE goal_push_delivery DROP COLUMN kind")


class WorkspaceConflict(Exception):
    pass


class WorkspaceMissing(Exception):
    pass
