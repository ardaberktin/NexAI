"""Persistent state for Nex's daily feed and ongoing goals."""

GOALS_TABLE = """
CREATE TABLE IF NOT EXISTS goals (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, prompt TEXT NOT NULL,
    zone TEXT NOT NULL, interval_minutes INTEGER NOT NULL, scheduled_at TEXT,
    status TEXT NOT NULL, created TEXT NOT NULL, updated TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1, next_run TEXT, last_checked TEXT, error TEXT, latest TEXT,
    requested INTEGER NOT NULL DEFAULT 0, watch_provider TEXT,
    original_request TEXT NOT NULL DEFAULT '', reply_language TEXT, pdf_instruction TEXT,
    plan_state TEXT
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
CREATE TABLE IF NOT EXISTS goal_update_pdfs (
    activity_id TEXT PRIMARY KEY, goal_id TEXT NOT NULL, brief TEXT NOT NULL,
    title TEXT NOT NULL, language TEXT, retry_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS goal_update_pdfs_due ON goal_update_pdfs(retry_at);
CREATE INDEX IF NOT EXISTS goal_update_pdfs_goal ON goal_update_pdfs(goal_id);
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
    cancelled INTEGER NOT NULL DEFAULT 0, pdf_instruction TEXT, pdf_retry_at TEXT
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
CREATE TABLE IF NOT EXISTS iphone_push_session (
    singleton INTEGER PRIMARY KEY REFERENCES iphone_push_registration(singleton) ON DELETE CASCADE,
    device_id TEXT NOT NULL
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
CREATE TABLE IF NOT EXISTS pending_actions (
    id TEXT PRIMARY KEY,
    source_type TEXT NOT NULL CHECK(source_type IN ('chat','goal')),
    source_id TEXT NOT NULL,
    goal_id TEXT,
    tool_alias TEXT NOT NULL,
    server_name TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    operation TEXT NOT NULL,
    arguments_json TEXT NOT NULL,
    summary TEXT NOT NULL,
    tainted INTEGER NOT NULL DEFAULT 0,
    reason TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK(status IN ('pending','approved_once','approved_goal','denied','executed','failed')),
    receipt_json TEXT,
    created TEXT NOT NULL,
    updated TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS pending_actions_status ON pending_actions(status, created DESC);
CREATE INDEX IF NOT EXISTS pending_actions_source ON pending_actions(source_type, source_id);
CREATE TABLE IF NOT EXISTS goal_action_grants (
    goal_id TEXT NOT NULL,
    tool_alias TEXT NOT NULL,
    granted TEXT NOT NULL,
    PRIMARY KEY(goal_id, tool_alias)
);
CREATE TABLE IF NOT EXISTS memory_facts (
    id TEXT PRIMARY KEY,
    fact_key TEXT UNIQUE NOT NULL,
    display_key TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'fact',
    content TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 1.0,
    source_id TEXT NOT NULL DEFAULT '',
    created TEXT NOT NULL,
    updated TEXT NOT NULL,
    expires_at TEXT,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS memory_facts_active ON memory_facts(active, updated DESC);
CREATE VIRTUAL TABLE IF NOT EXISTS memory_facts_fts USING fts5(
    display_key, content, category,
    content='memory_facts', content_rowid='rowid',
    tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS memory_facts_ai AFTER INSERT ON memory_facts BEGIN
    INSERT INTO memory_facts_fts(rowid, display_key, content, category)
    VALUES (new.rowid, new.display_key, new.content, new.category);
END;
CREATE TRIGGER IF NOT EXISTS memory_facts_ad AFTER DELETE ON memory_facts BEGIN
    INSERT INTO memory_facts_fts(memory_facts_fts, rowid, display_key, content, category)
    VALUES ('delete', old.rowid, old.display_key, old.content, old.category);
END;
CREATE TRIGGER IF NOT EXISTS memory_facts_au AFTER UPDATE OF display_key, content, category ON memory_facts BEGIN
    INSERT INTO memory_facts_fts(memory_facts_fts, rowid, display_key, content, category)
    VALUES ('delete', old.rowid, old.display_key, old.content, old.category);
    INSERT INTO memory_facts_fts(rowid, display_key, content, category)
    VALUES (new.rowid, new.display_key, new.content, new.category);
END;
CREATE TABLE IF NOT EXISTS memory_vectors (
    fact_id TEXT PRIMARY KEY REFERENCES memory_facts(id) ON DELETE CASCADE,
    dim INTEGER NOT NULL,
    model TEXT NOT NULL,
    vector BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS workspace_skills (
    id TEXT PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    description TEXT NOT NULL,
    parameters_json TEXT NOT NULL DEFAULT '{}',
    source TEXT NOT NULL,
    approved INTEGER NOT NULL DEFAULT 0,
    created TEXT NOT NULL,
    updated TEXT NOT NULL,
    last_run TEXT,
    run_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS workspace_skills_approved ON workspace_skills(approved, name);
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
    card_columns = {column["name"] for column in db.execute("PRAGMA table_info(goal_follow_ups)")}
    for column in ("pdf_instruction", "pdf_retry_at"):
        if column not in card_columns:
            db.execute(f"ALTER TABLE goal_follow_ups ADD COLUMN {column} TEXT")
    db.execute("CREATE INDEX IF NOT EXISTS goal_follow_up_pdf_due ON goal_follow_ups(pdf_retry_at) "
               "WHERE pdf_retry_at IS NOT NULL")
    migrate_goal_context(db)
    db.execute("CREATE INDEX IF NOT EXISTS goal_work_request ON goal_follow_up_work(chat_request_id) "
               "WHERE chat_request_id IS NOT NULL")
    db.execute("CREATE INDEX IF NOT EXISTS goal_notice_request ON goal_follow_up_notices(chat_request_id) "
               "WHERE chat_request_id IS NOT NULL")


def migrate_goal_context(db):
    columns = {column["name"] for column in db.execute("PRAGMA table_info(goals)")}
    if "original_request" not in columns:
        db.execute("ALTER TABLE goals ADD COLUMN original_request TEXT NOT NULL DEFAULT ''")
        db.execute("UPDATE goals SET original_request=COALESCE((SELECT request FROM goal_follow_up_work "
                   "WHERE goal_id=goals.id), '')")
    if "reply_language" not in columns:
        db.execute("ALTER TABLE goals ADD COLUMN reply_language TEXT")
        db.execute("UPDATE goals SET reply_language=(SELECT language FROM goal_follow_up_work WHERE goal_id=goals.id)")
    if "pdf_instruction" not in columns:
        db.execute("ALTER TABLE goals ADD COLUMN pdf_instruction TEXT")
    if "plan_state" not in columns:
        db.execute("ALTER TABLE goals ADD COLUMN plan_state TEXT")


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
