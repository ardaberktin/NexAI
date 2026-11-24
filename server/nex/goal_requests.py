"""Recognize and save goal requests from private Chat."""

import re
from dataclasses import dataclass
from datetime import UTC, datetime

from .goal_schedule import DEFAULT_TIME, HOURLY, next_local_run, requested_clock
from .goals import Goals
from .recall_dates import time_zone
from .reminder_intent import (
    ASSIGNMENTS,
    CONDITION,
    INBOX,
    MANUAL_MAIL,
    PACKAGE,
    PREFIX,
    ReminderRequest,
    infer_reminder,
    package_question,
    reminder_request,
)
from .reminder_schedule import ReminderTimeError, default_check_in, reminder_timing
from .wake import spoken_wake_body
from .workspace_features import FeatureDisabled, is_enabled, off_message

POLITE = re.compile(r"^(?:(?:please|(?:can|could|would|will) you|i(?: want| need|['’]d like) you to)[\s,]+)+",
                    re.IGNORECASE)
NEW_GOAL = re.compile(
    r"(?:create|add|make|set up) (?:me )?(?:a |an )?(?:new )?(?:(?P<email>e-?mail|gmail) )?goal"
    r"(?: (?P<link>to|for|about|called|named)(?:\s+(?P<description>.+))?)?$", re.IGNORECASE | re.DOTALL,
)
MAIL_SOURCE = r"(?:gmail(?: inbox| messages?)?|inbox|e-?mails?|mail)"
MAIL_CHECK = re.compile(
    rf"^(?:check(?: on)?|watch|monitor|look(?: for)?|search|scan|read|review)\s+"
    rf"(?:(?:my |the )?{MAIL_SOURCE} (?:for|about|regarding) (?P<after>.+)"
    rf"|(?P<before>.+?)\s+(?:in|from) (?:my |the )?{MAIL_SOURCE})$", re.IGNORECASE | re.DOTALL,
)
WATCH = re.compile(rf"watch (?:my |the )?{MAIL_SOURCE} for\s+(.+)", re.IGNORECASE)
CADENCES = {"every 15 minutes": 15, "hourly": HOURLY, "every hour": HOURLY,
            "daily": 1440, "every day": 1440, "every morning": 1440,
            "weekly": 10080, "every week": 10080}
CADENCE = re.compile(r"\s+(" + "|".join(CADENCES) + r")$", re.IGNORECASE)
REPEAT = re.compile(
    r"\b(every (?:15 minutes|hour|day|morning|week)|"
    r"(?:hourly|daily|weekly)(?=\s+(?:at|to|on)\b|\s*$))\b", re.IGNORECASE,
)


@dataclass(frozen=True)
class GoalRequest:
    prompt: str
    interval: int = 0
    scheduled_at: datetime | None = None


def mail_goal(subject, interval):
    prompt = f"Notify me about new Gmail messages matching this request: {subject}" if subject else ""
    return GoalRequest(prompt, interval)


def split_cadence(subject):
    if cadence := CADENCE.search(subject):
        return subject[:cadence.start()].rstrip(", "), CADENCES[cadence[1].casefold()]
    return subject, HOURLY


def goal_request(text):
    text = text.strip()
    command = POLITE.sub("", (spoken_wake_body(text) or text).rstrip(".!?").strip())
    if match := NEW_GOAL.fullmatch(command):
        description = (match["description"] or "").strip()
        subject, interval = split_cadence(description)
        if mail := MAIL_CHECK.fullmatch(subject):
            return mail_goal(mail["before"] or mail["after"], interval)
        if match["email"]:
            return mail_goal(subject, interval)
        if (match["link"] or "").lower() == "to" and (reminder := reminder_request(description)):
            return reminder
        return GoalRequest(description)
    if watch := WATCH.fullmatch(command):
        subject, interval = split_cadence(watch[1].strip())
        return mail_goal(subject, interval)
    return reminder_request(command)


def check_in_goal(subject, due):
    topic = CONDITION.sub("", subject)
    question = package_question(topic) or f"Checking in about “{topic}”. How did it go?"
    return GoalRequest(f"Ask me this check-in question: {question}", scheduled_at=due)


def resolve_reminder(request, local, classify):
    if cadence := REPEAT.search(request.body):
        body = request.body[:cadence.start()].strip() + " " + request.body[cadence.end():].strip()
        body = body.strip()
        interval = CADENCES[cadence[1].casefold()]
        if not request.check_in and not MANUAL_MAIL.match(body) and (INBOX.search(body) or ASSIGNMENTS.search(body)):
            return mail_goal(body, interval)
        subject, due = reminder_timing(body, local)
        if subject:
            return GoalRequest(f"{request.cue.capitalize()} {subject}", interval, due)
    body, scheduled = reminder_timing(request.body, local)
    subject = PREFIX.sub("", body)
    condition = CONDITION.match(subject)
    check_in = request.check_in or bool(condition)
    if not subject:
        return GoalRequest("")
    if scheduled:
        if check_in:
            return check_in_goal(subject, scheduled)
        return GoalRequest(f"Remind me to {subject}", scheduled_at=scheduled)
    if not request.check_in and MANUAL_MAIL.match(body):
        return GoalRequest(subject)
    subject, interval = split_cadence(subject)
    if not request.check_in and (INBOX.search(subject) or ASSIGNMENTS.search(body)):
        return mail_goal(subject, interval)
    if request.check_in or (condition and PACKAGE.search(subject)):
        return check_in_goal(subject, default_check_in(local))
    inferred = infer_reminder(request, body, classify)
    if inferred == "EMAIL":
        return mail_goal(subject, interval)
    if inferred == "CHECK" or condition:
        return check_in_goal(subject, default_check_in(local))
    return GoalRequest(PREFIX.sub("", body))


def prompt_schedule(request, local, now):
    if request.scheduled_at:
        return request.prompt, request.scheduled_at, request.interval
    subject = request.prompt
    cadence = REPEAT.search(subject)
    interval = CADENCES[cadence[1].casefold()] if cadence else request.interval
    timing = REPEAT.sub("", subject).strip(" ,")
    prompt, start = reminder_timing(timing, local)
    if interval and start is None:
        start = next_local_run(DEFAULT_TIME, local.tzinfo.key, now) if interval >= 1440 else now
    return prompt, start, interval


def resolve_request(text, action, local, now, classify):
    if action == "daily_report":
        clock = requested_clock(text, classify)
        return GoalRequest(text, 1440, next_local_run(clock, local.tzinfo.key, now))
    if action == "email_watch":
        return mail_goal(text, HOURLY)
    request = ReminderRequest("check in with me", text) if action == "check_in" else goal_request(text)
    return resolve_reminder(request, local, classify) if isinstance(request, ReminderRequest) else request


def create_goal(store, identifier, text, zone, *, asked_at=None, classify=None, action=None):
    if not is_enabled(store, "goals"):
        return {"text": off_message("goals"), "sources": []}
    now = datetime.now(UTC)
    local = (asked_at or now).astimezone(time_zone(zone))
    try:
        request = resolve_request(text, action, local, now, classify)
        prompt, start, interval = prompt_schedule(request, local, now)
    except ReminderTimeError:
        return {"text": "What date and time should I use for the goal?", "sources": []}
    if not prompt:
        return {"text": "What should Nex do for this goal?", "sources": []}
    if len(prompt) > 1200:
        return {"text": "Describe the goal in 1,200 characters or fewer.", "sources": []}
    if start and ((start <= now and not interval) or start.year > 2100):
        return {"text": "Choose a future time for the goal.", "sources": []}
    data = {"prompt": prompt, "zone": zone, "frequency_minutes": interval,
            "scheduled_at": start.isoformat() if start else None}
    try:
        goal = Goals(store).save(identifier, data, anchor=now, create_once=True, request=text)
    except FeatureDisabled as exc:
        return {"text": str(exc), "sources": []}
    answer = f"I created “{goal['title']}”."
    if goal["scheduled_at"]:
        due = datetime.fromisoformat(goal["scheduled_at"]).astimezone(time_zone(goal["zone"]))
        cadence = {15: "every 15 minutes", HOURLY: "every hour", 1440: "every day",
                   10080: "every week"}.get(goal["frequency_minutes"])
        first = f"{due:%A, %B} {due.day} at {due:%-I:%M %p} your time"
        answer += f" I’ll run it {cadence} starting {first}." if cadence else f" I’ll run it on {first}."
    else:
        answer += " You can run it on demand or add a time."
    answer += " You can edit the goal in Goals."
    return {"text": answer, "sources": [], "goal_id": goal["id"]}
