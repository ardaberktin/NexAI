"""Persistent state for Nex's daily feed and ongoing goals."""

GOALS_TABLE = """
CREATE TABLE IF NOT EXISTS goals (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, prompt TEXT NOT NULL,
    zone TEXT NOT NULL, interval_minutes INTEGER NOT NULL, scheduled_at TEXT,
    status TEXT NOT NULL, created TEXT NOT NULL, updated TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1, next_run TEXT, last_checked TEXT, error TEXT, latest TEXT,
    requested INTEGER NOT NULL DEFAULT 0, watch_provider TEXT
)"""
GOALS_INDEX = "CREATE INDEX IF NOT EXISTS goals_due ON goals(status, next_run)"

GOAL_ACTIVITY_TABLE = """
CREATE TABLE IF NOT EXISTS goal_activity (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE, goal_id TEXT NOT NULL, created TEXT NOT NULL,
    title TEXT NOT NULL, detail TEXT NOT NULL, artifact INTEGER NOT NULL DEFAULT 0
)"""
GOAL_ACTIVITY_INDEXES = (
    "CREATE INDEX IF NOT EXISTS goal_activity_recent ON goal_activity(goal_id, created DESC)",
    "CREATE INDEX IF NOT EXISTS goal_activity_chat_page ON goal_activity(created DESC, id DESC) WHERE artifact=1",
)

WORKSPACE_SCHEMA = ";\n".join((GOALS_TABLE, GOALS_INDEX, GOAL_ACTIVITY_TABLE, *GOAL_ACTIVITY_INDEXES)) + ";\n" + """
CREATE TABLE IF NOT EXISTS pdf_artifacts (
    id TEXT PRIMARY KEY, goal_id TEXT, created TEXT NOT NULL,
    title TEXT NOT NULL, pages INTEGER NOT NULL, pdf BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS pdf_artifacts_goal ON pdf_artifacts(goal_id, created DESC);
CREATE INDEX IF NOT EXISTS pdf_artifacts_recent ON pdf_artifacts(created DESC);
CREATE TABLE IF NOT EXISTS clock_alerts (
    id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK(kind IN ('timer','alarm')), label TEXT NOT NULL,
    created_at TEXT NOT NULL, due_at TEXT NOT NULL, zone TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('scheduled','fired','cancelled'))
);
CREATE INDEX IF NOT EXISTS clock_alerts_due ON clock_alerts(status,due_at);
CREATE TABLE IF NOT EXISTS clock_native_alarms (
    alarm_id TEXT PRIMARY KEY, installation TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS clock_native_installation ON clock_native_alarms(installation);
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
CREATE TABLE IF NOT EXISTS workspace_ideas (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    items TEXT NOT NULL DEFAULT '[]', pending_id TEXT, failed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS deleted_goal_ids (
    id TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS goal_follow_up_work (
    goal_id TEXT PRIMARY KEY, request TEXT NOT NULL, language TEXT,
    answered_id TEXT, next_try TEXT, required INTEGER NOT NULL DEFAULT 0, chat_request_id TEXT
);
CREATE INDEX IF NOT EXISTS goal_follow_up_work_due ON goal_follow_up_work(next_try);
CREATE TABLE IF NOT EXISTS goal_follow_ups (
    id TEXT PRIMARY KEY, goal_id TEXT NOT NULL, due TEXT NOT NULL,
    question TEXT NOT NULL, choices TEXT NOT NULL, uses_goal_run INTEGER NOT NULL,
    published_at TEXT, selected_choice INTEGER, answered_at TEXT,
    cancelled INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS goal_follow_ups_by_goal ON goal_follow_ups(goal_id);
CREATE INDEX IF NOT EXISTS goal_follow_ups_due ON goal_follow_ups(due)
    WHERE published_at IS NULL AND cancelled=0;
CREATE UNIQUE INDEX IF NOT EXISTS goal_follow_ups_open ON goal_follow_ups(goal_id)
    WHERE selected_choice IS NULL AND cancelled=0;
CREATE TABLE IF NOT EXISTS goal_follow_up_notices (
    follow_up_id TEXT PRIMARY KEY, next_try TEXT, created TEXT, text TEXT, chat_request_id TEXT
);
CREATE INDEX IF NOT EXISTS goal_follow_up_notices_due ON goal_follow_up_notices(next_try)
    WHERE next_try IS NOT NULL;
CREATE INDEX IF NOT EXISTS goal_follow_up_notices_chat ON goal_follow_up_notices(created DESC, follow_up_id DESC)
    WHERE text IS NOT NULL;
CREATE TABLE IF NOT EXISTS iphone_push_registration (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1), installation TEXT NOT NULL, token TEXT NOT NULL,
    environment TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS goal_push_delivery (
    id TEXT PRIMARY KEY, token TEXT NOT NULL,
    next_try INTEGER NOT NULL, expires INTEGER NOT NULL, attempts INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS goal_push_due ON goal_push_delivery(next_try);
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


def migrate_workspace(db, root):
    db.execute("BEGIN IMMEDIATE")
    feed_columns = {column["name"] for column in db.execute("PRAGMA table_info(feed_settings)")}
    for column in ("prompt", "deferred_until_day"):
        if column not in feed_columns:
            db.execute(f"ALTER TABLE feed_settings ADD COLUMN {column} TEXT")
    goal_columns = {column["name"] for column in db.execute("PRAGMA table_info(goals)")}
    if "kind" in goal_columns:
        from .goal_migration import migrate_goals

        migrate_goals(db)
    elif "watch_provider" not in goal_columns:
        db.execute("ALTER TABLE goals ADD COLUMN watch_provider TEXT")
    if "sequence" not in {column["name"] for column in db.execute("PRAGMA table_info(goal_activity)")}:
        migrate_goal_activity(db)
    delivery_columns = {column["name"] for column in db.execute("PRAGMA table_info(goal_push_delivery)")}
    if "kind" in delivery_columns:
        db.execute("ALTER TABLE goal_push_delivery DROP COLUMN kind")
    follow_up_columns = {column["name"] for column in db.execute("PRAGMA table_info(goal_follow_up_work)")}
    if "required" not in follow_up_columns:
        db.execute("ALTER TABLE goal_follow_up_work ADD COLUMN required INTEGER NOT NULL DEFAULT 0")
    if "chat_request_id" not in follow_up_columns:
        from .goal_chat_receipts import migrate_chat_receipts

        migrate_chat_receipts(db, root)
    db.execute("CREATE INDEX IF NOT EXISTS goal_work_request ON goal_follow_up_work(chat_request_id) "
               "WHERE chat_request_id IS NOT NULL")
    db.execute("CREATE INDEX IF NOT EXISTS goal_notice_request ON goal_follow_up_notices(chat_request_id) "
               "WHERE chat_request_id IS NOT NULL")


def migrate_goal_activity(db):
    # Keep existing Mac alert cursors valid and prevent sequence reuse after deletion.
    db.execute("ALTER TABLE goal_activity RENAME TO legacy_goal_activity")
    db.execute(GOAL_ACTIVITY_TABLE)
    db.execute(
        "INSERT INTO goal_activity(sequence,id,goal_id,created,title,detail,artifact) "
        "SELECT rowid,id,goal_id,created,title,detail,artifact FROM legacy_goal_activity"
    )
    db.execute("DROP TABLE legacy_goal_activity")
    for statement in GOAL_ACTIVITY_INDEXES:
        db.execute(statement)


class WorkspaceConflict(Exception):
    pass


class WorkspaceMissing(Exception):
    pass
