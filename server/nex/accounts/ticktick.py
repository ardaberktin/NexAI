"""Read TickTick projects and tasks through the Open API."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time
from urllib.parse import quote

from ..recall_dates import time_zone
from ..store import normalized
from .credentials import TICK_TASKS
from .google import clean
from .http import AccountError, AccountHTTPError, request

PROJECTS_URL = "https://api.ticktick.com/open/v1/project"


class TickTick:
    def __init__(self, vault):
        self.token = vault.token("ticktick", {TICK_TASKS})

    def projects(self):
        result, _ = request(PROJECTS_URL, token=self.token)
        return read_rows(result)

    def tasks(self, project_id):
        url = PROJECTS_URL + "/" + quote(project_id, safe="") + "/data"
        try:
            result, _ = request(url, token=self.token)
        except AccountHTTPError as error:
            if error.status == 404:
                # A list can disappear between the project and task requests.
                return []
            raise
        if not isinstance(result, dict):
            raise AccountError("TickTick returned unexpected project data.")
        return read_rows(result.get("tasks"))


def read_rows(result):
    if not isinstance(result, list):
        raise AccountError("TickTick returned an unexpected list response.")
    return [item for item in result if isinstance(item, dict)]


def task_time(item, field, zone):
    raw = item.get(field)
    if not isinstance(raw, str):
        return None
    try:
        stamp = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if item.get("isAllDay"):
        if stamp.tzinfo is not None:
            try:
                stamp = stamp.astimezone(time_zone(item.get("timeZone")))
            except ValueError:
                pass
        return datetime.combine(stamp.date(), time(), zone)
    return stamp.replace(tzinfo=zone) if stamp.tzinfo is None else stamp.astimezone(zone)


def matches_topic(item, topic):
    fields = (item.get(key) for key in ("title", "content", "desc"))
    return any(isinstance(value, str) and topic in normalized(" ".join(value.split())) for value in fields)


def matching_tasks(rows, topic, start, end):
    topic = normalized(topic)
    matches = []
    for item in rows:
        if item.get("status", 0) != 0 or item.get("kind") == "NOTE":
            continue
        due = task_time(item, "dueDate", start.tzinfo)
        begins = task_time(item, "startDate", start.tzinfo) if due is None else None
        stamp = due or begins
        if topic and not matches_topic(item, topic):
            continue
        if not topic and (stamp is None or not start <= stamp < end):
            continue
        task = {"title": clean(item.get("title"), 140) or "Untitled task", "priority": item.get("priority")}
        if stamp is not None:
            field = "due" if due is not None else "start"
            task[field] = stamp.strftime("%Y-%m-%d (all day)" if item.get("isAllDay") else "%Y-%m-%d %H:%M")
        matches.append((stamp, task))
    matches.sort(key=lambda match: (match[0] is None, match[0]))
    return [task for _, task in matches[:18]]


def ticktick_items(vault, intent, start, end):
    client = TickTick(vault)
    projects = [item for item in client.projects() if not item.get("closed")]
    if intent.lists:
        names = [clean(item.get("name"), 130) for item in projects]
        return [{"title": name} for name in names if name][:20]
    ids = ["inbox", *(item["id"] for item in projects if isinstance(item.get("id"), str) and item["id"])]
    with ThreadPoolExecutor(max_workers=8) as pool:
        rows = (item for tasks in pool.map(client.tasks, dict.fromkeys(ids)) for item in tasks)
        return matching_tasks(rows, intent.topic, start, end)
