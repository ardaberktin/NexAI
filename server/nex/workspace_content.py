"""Read workspace content built from Nex's existing archive and scheduled work."""

import hashlib
import json

from .accounts.credentials import Credentials
from .conversation_history import SHARED_QUESTION, TURN_ID
from .goals import Goals
from .store import CURRENT_EVIDENCE
from .workspace_features import enabled


def idea(identifier, icon, title, detail, prompt, *, opens_goal=False):
    return {"id": identifier, "icon": icon, "title": title, "detail": detail, "prompt": prompt,
            "opens_goal": opens_goal}


def ideas(store):
    with store.connect() as db:
        feed_on = enabled(db, "feed")
        goals_on = enabled(db, "goals")
        settings = db.execute("SELECT topics, prompt FROM feed_settings").fetchone() if feed_on else None
        saved = db.execute("SELECT story FROM feed_saved ORDER BY saved DESC LIMIT 1").fetchone() if feed_on else None
    goals = [goal for goal in Goals(store).overview() if goal["status"] == "active"] if goals_on else []
    connections = Credentials(store.root).status()
    picks = []
    if goals:
        goal = goals[0]
        picks.append(idea(
            "next-step", "target", "Pick a next step for your goal",
            f"Make a plan you can start today for “{goal['title']}”",
            f"Help me pick one specific next step for this goal: {goal['prompt']}",
        ))
    if saved:
        story = json.loads(saved["story"])
        picks.append(idea("saved-story", "newspaper", "Get the context behind a saved story",
                          story["title"], f"Read {story['url']} and explain the context behind this story."))
    elif settings and settings["prompt"]:
        picks.append(idea("news", "newspaper", "Go deeper on your news interests",
                          "Find one current story based on your feed prompt.",
                          f"Find a recent news story matching these interests and explain it in depth: {settings['prompt']}"))
    elif settings:
        topics = ", ".join(json.loads(settings["topics"])[:3])
        picks.append(idea("news", "newspaper", "Go deeper on your news interests",
                          f"Find a current story worth following across {topics}.",
                          f"Search the web for recent news about {topics} and explain one story in depth."))
    if goals_on and connections["gmail"] == "connected":
        picks.append(idea("inbox", "envelope", "Watch for an email you’re expecting",
                          "Describe the emails you care about and Nex will alert you when they arrive.",
                          "Notify me when a new Gmail message mentions a deadline.", opens_goal=True))
    if goals_on and connections["calendar"] == "connected":
        picks.append(idea("calendar", "calendar", "Keep an eye on the next seven days",
                          "Track upcoming events on your connected Google calendars.",
                          "Show me my upcoming Google Calendar events over the next seven days.", opens_goal=True))
    picks.append(idea("week", "list.bullet.rectangle", "Turn recent conversations into a checklist",
             "Pull decisions and unfinished work from your recorded conversations into a short plan.",
             "Review my recorded conversations from the past seven days and list open commitments and next steps."))
    if goals_on:
        picks.append(idea("reminder", "bell", "Remember something at the right time",
                          "Set a timed reminder for an appointment, a deadline, or a follow-up.",
                          "Remind me to review tomorrow’s plans.",
                          opens_goal=True))
    picks.append(idea("decision", "arrow.triangle.branch", "Think through a decision",
             "Use a simple comparison to narrow your options and choose a next step.",
             "Help me think through a decision. Ask what I’m deciding, what the options are, and what matters most."))
    return picks


def chat_history(store):
    with store.connect() as db:
        exchanges = db.execute(
            f"""SELECT {TURN_ID} AS id,q.body AS question,q.started AS asked,
                       m.body AS answer,m.started AS answered,m.sources
                FROM memory q LEFT JOIN memory m ON m.id='a-'||{TURN_ID} AND m.kind='answer'
                WHERE {SHARED_QUESTION} AND {CURRENT_EVIDENCE}
                ORDER BY q.started DESC,q.number DESC LIMIT 30""",
        ).fetchall()
        updates = db.execute(
            "SELECT a.id,a.created,a.title,a.detail,a.goal_id,g.title AS goal "
            "FROM goal_activity a JOIN goals g ON a.goal_id=g.id WHERE a.artifact=1 "
            "ORDER BY a.created DESC,a.rowid DESC LIMIT 30",
        ).fetchall()
    messages = []
    for row in exchanges:
        identifier = row["id"]
        messages.append({"id": "q-" + identifier, "role": "user", "text": row["question"],
                         "date": row["asked"], "sources": []})
        if row["answer"] is not None:
            messages.append({"id": "a-" + identifier, "role": "assistant", "text": row["answer"],
                             "date": max(row["asked"], row["answered"]),
                             "sources": [source for source in json.loads(row["sources"]) if source != identifier]})
    for row in updates:
        messages.append({
            "id": row["id"], "role": "update", "text": row["detail"], "date": row["created"], "sources": [],
            "title": row["goal"] + " · " + row["title"], "goal_id": row["goal_id"],
        })
    return sorted(messages, key=lambda item: (item["date"], item["role"] != "user", item["id"]))


def chat_snapshot(store, cursor):
    messages = chat_history(store)
    version = hashlib.sha256(json.dumps(messages, sort_keys=True).encode()).hexdigest()
    if cursor == version:
        return {"cursor": version}
    return {"cursor": version, "items": messages}


def artifacts(store, goal=None):
    with store.connect() as db:
        if goal:
            Goals.find(db, goal)
            editions = []
        else:
            editions = db.execute("SELECT * FROM feed_editions ORDER BY day DESC LIMIT 30").fetchall()
        updates = db.execute(
            "SELECT a.id,a.created,a.title,a.detail,a.goal_id,g.title AS goal "
            "FROM goal_activity a JOIN goals g ON a.goal_id=g.id WHERE a.artifact=1 "
            "AND (? IS NULL OR a.goal_id=?) ORDER BY a.created DESC,a.rowid DESC LIMIT 50", (goal, goal),
        ).fetchall()
    files = [edition_artifact(row) for row in editions]
    files.extend({
        "id": row["id"], "title": row["goal"], "subtitle": row["title"], "date": row["created"], "kind": "goal",
        "text": row["detail"], "goal_id": row["goal_id"],
    } for row in updates)
    return sorted(files, key=lambda item: item["date"], reverse=True)


def edition_artifact(row):
    title = "Daily news · " + row["day"]
    sections = [title]
    for story in json.loads(row["stories"]):
        sections.append("\n".join((story["title"], story["source"], story["summary"], story["url"])))
    return {"id": "feed-" + row["day"], "title": title, "subtitle": "Feed edition",
                "date": row["generated"], "kind": "feed", "text": "\n\n".join(sections)}
