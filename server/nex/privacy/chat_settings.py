"""Account defaults for keeping otherwise ordinary typed exchanges private."""

DEFAULTS = {"enabled": True, "general": False, "web": False, "python": False, "weather": False}


def settings(state):
    with state.transaction() as db:
        return snapshot(db)


def snapshot(db):
    return DEFAULTS | {row["name"]: bool(row["enabled"]) for row in db.execute("SELECT * FROM chat_privacy")}


def update(state, name, enabled):
    if not isinstance(name, str) or name not in DEFAULTS or type(enabled) is not bool:
        raise ValueError("Choose a Chat privacy setting and an on or off value.")
    with state.transaction(write=True) as db:
        db.execute("INSERT INTO chat_privacy VALUES(?,?) ON CONFLICT(name) DO UPDATE SET enabled=excluded.enabled",
                   (name, enabled))
        return snapshot(db)


def prefers_private(preferences, plan):
    categories = {tool["name"] for tool in plan.tools} or {"general"}
    return any(preferences.get(name, False) for name in categories)
