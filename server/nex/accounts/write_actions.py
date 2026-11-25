"""Translate validated connected-account actions into bounded provider requests."""

import base64
from datetime import date, datetime
from email.errors import HeaderParseError
from email.headerregistry import Address
from email.message import EmailMessage
from email.policy import SMTP
from urllib.parse import quote

from .http import AccountError, request
from .ticktick import PROJECTS_URL, TickTick

GMAIL_SEND = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
CALENDAR_EVENTS = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
TICK_TASKS_URL = "https://api.ticktick.com/open/v1/task"


def confirmed_id(result):
    identifier = result.get("id") if isinstance(result, dict) else None
    if not isinstance(identifier, str) or not identifier or len(identifier) > 512:
        raise AccountError("The provider did not return an ID confirming the change. Check the account before retrying.")
    return identifier


def send_email(token, args):
    try:
        address = Address(addr_spec=args["to"])
        if not address.domain or not address.username or address.addr_spec != args["to"]:
            raise ValueError("Expected one plain address.")
        message = EmailMessage(policy=SMTP)
        message["To"] = address
        message["Subject"] = args["subject"]
        message.set_content(args["body"])
    except (HeaderParseError, IndexError, ValueError):
        raise AccountError("Supply a single email address and a valid subject and message.") from None
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
    result, _ = request(GMAIL_SEND, token=token, payload={"raw": raw})
    return {"id": confirmed_id(result), "to": args["to"], "subject": args["subject"]}


def event_times(start, end):
    try:
        if len(start) == len(end) == 10:
            starts, ends = date.fromisoformat(start), date.fromisoformat(end)
            if ends <= starts:
                raise ValueError("End must follow start.")
            return {"date": starts.isoformat()}, {"date": ends.isoformat()}
        starts, ends = datetime.fromisoformat(start), datetime.fromisoformat(end)
        if starts.tzinfo is None or ends.tzinfo is None or ends <= starts:
            raise ValueError("Expected ordered times with offsets.")
        return {"dateTime": starts.isoformat()}, {"dateTime": ends.isoformat()}
    except ValueError:
        raise AccountError("Supply an end after the start and use either ISO dates or timestamps with UTC offsets.") from None


def create_event(token, args):
    start, end = event_times(args["start"], args["end"])
    payload = {"summary": args["title"], "start": start, "end": end}
    payload.update({key: args[key] for key in ("description", "location") if key in args})
    result, _ = request(CALENDAR_EVENTS, token=token, payload=payload)
    return {"id": confirmed_id(result), "title": args["title"], "start": start, "end": end}


def path_id(value):
    if not isinstance(value, str) or not value or len(value) > 256 or value in {".", ".."}:
        raise AccountError("TickTick did not return a usable item ID.")
    return quote(value, safe="")


def project_id(client, name):
    if name is None or name.casefold() == "inbox":
        return "inbox"
    matches = [item for item in client.projects() if not item.get("closed")
               and str(item.get("name", "")).casefold() == name.casefold()]
    if len(matches) != 1:
        raise AccountError("TickTick did not return exactly one open list with this name.")
    identifier = matches[0].get("id")
    path_id(identifier)
    return identifier


def create_task(token, args):
    project = project_id(TickTick(token), args.get("list"))
    payload = {"title": args["title"]}
    if project != "inbox":
        payload["projectId"] = project
    if "content" in args:
        payload["content"] = args["content"]
    if due := args.get("due"):
        try:
            stamp = datetime.fromisoformat(due)
            if stamp.tzinfo is None:
                raise ValueError("Expected an offset.")
            payload["dueDate"] = stamp.strftime("%Y-%m-%dT%H:%M:%S%z")
        except ValueError:
            raise AccountError("Supply the TickTick due date as a timestamp with its UTC offset.") from None
    result, _ = request(TICK_TASKS_URL, token=token, payload=payload)
    return {"id": confirmed_id(result), "title": args["title"], "list": args.get("list", "Inbox")}


def complete_task(token, args):
    ticktick = TickTick(token)
    project = project_id(ticktick, args.get("list"))
    matches = [task for task in ticktick.tasks(project) if task.get("status", 0) == 0 and task.get("kind") != "NOTE"
               and str(task.get("title", "")).casefold() == args["title"].casefold()]
    if len(matches) != 1:
        raise AccountError("TickTick did not return exactly one open task with this title in the selected list.")
    task = matches[0]
    actual_project = path_id(task.get("projectId", project))
    url = f"{PROJECTS_URL}/{actual_project}/task/{path_id(task.get('id'))}/complete"
    request(url, token=token, method="POST")
    return {"id": task["id"], "title": args["title"], "list": args.get("list", "Inbox")}
