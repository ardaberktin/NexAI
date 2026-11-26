"""Shared feature switches for the archive and live services."""

from datetime import UTC, datetime

FEATURES = ("daybook", "activities", "feed", "ideas", "goals")

SCHEMA = """
CREATE TABLE IF NOT EXISTS workspace_features (
    name TEXT PRIMARY KEY, enabled INTEGER NOT NULL, revision INTEGER NOT NULL
);
"""


def snapshot(db):
    return dict.fromkeys(FEATURES, True) | {
        row["name"]: bool(row["enabled"]) for row in db.execute("SELECT name,enabled FROM workspace_features")
    }


def revision(db, feature):
    row = db.execute("SELECT enabled,revision FROM workspace_features WHERE name=?", (feature,)).fetchone()
    if row is None:
        return 0
    return row["revision"] if row["enabled"] else None


def enabled(db, feature):
    return revision(db, feature) is not None


def is_enabled(store, feature):
    with store.connect() as db:
        return enabled(db, feature)


def settings(store):
    with store.connect() as db:
        return snapshot(db)


def update(store, feature, value):
    if feature not in FEATURES or type(value) is not bool:
        raise ValueError("Choose a workspace feature and an on or off value.")
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if enabled(db, feature) != value:
            db.execute(
                "INSERT INTO workspace_features VALUES(?,?,1) "
                "ON CONFLICT(name) DO UPDATE SET enabled=excluded.enabled,revision=revision+1", (feature, value),
            )
            if feature in {"daybook", "activities"}:
                due = datetime.now(UTC).isoformat(timespec="milliseconds")
                db.execute("UPDATE daybook_reports SET version=version+1,due=? "
                           "WHERE ?='activities' OR due IS NOT NULL", (due, feature))
            if feature == "activities" and not value:
                db.execute("DELETE FROM activity_screen_samples")
        return snapshot(db)


def off_message(feature):
    return f"{feature.title()} is off. Turn it on in Settings → Workspace features."


class FeatureDisabled(Exception):
    def __init__(self, feature):
        super().__init__(off_message(feature))


def require(db, feature):
    if not enabled(db, feature):
        raise FeatureDisabled(feature)


def require_enabled(store, feature):
    with store.connect() as db:
        require(db, feature)
