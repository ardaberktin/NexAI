"""Synthetic goals, inbox and model transport for the goal audit."""

import asyncio
import socket
import sys
from datetime import datetime, timedelta
from unittest.mock import patch
from uuid import NAMESPACE_DNS, uuid5
from zoneinfo import ZoneInfo

from nex.accounts.read import prepare_account
from nex.goal_follow_ups import assessment, for_goal
from nex.goals import Goals
from nex.live.dialogue import Dialogue

ZONE = "America/Vancouver"
MAIL = {
    "opening": {"sender": "Northstar Recruiting", "subject": "Remote software engineer opening",
                "body": "Northstar is hiring a full-time remote software engineer. Apply by December 4."},
    "receipt": {"sender": "BoardMail", "subject": "Job board subscription receipt",
                "body": "Your subscription renewed. This is a payment receipt, not an opening."},
    "event": {"sender": "Meetup Club", "subject": "Career networking social",
              "body": "Join us for coffee with local engineers. No roles are being advertised."},
}
SEEDS = {
    "jobs": ("Job leads", "Watch Gmail for new emails containing job postings.", 60, 8, "gmail"),
    "rent": ("Rent listings", "Watch Gmail for new rental apartment listings.", 60, 9, "gmail"),
    "weekly": ("Weekly planning", "Summarize the upcoming week from my calendar.", 10080, 8, None),
    "budget": ("Weekly budget summary", "Summarize my budget notes for the week.", 10080, 10, None),
    "design": ("Design job watch", "Watch Gmail for new design job postings.", 1440, 8, "gmail"),
    "engineering": ("Engineering job watch", "Watch Gmail for new engineering job postings.", 1440, 8, "gmail"),
    "card": ("Card delivery", "Ask whether my physical replacement bank card arrived by post.", 0, 18, None),
}


def identifier(key):
    return str(uuid5(NAMESPACE_DNS, "nex-goal-audit." + key))


def seed(store, keys):
    local = datetime.now(ZoneInfo(ZONE))
    next_monday = local.date() + timedelta(days=(7 - local.weekday()) or 7)
    for key in keys:
        title, prompt, minutes, hour, provider = SEEDS[key]
        due = datetime.combine(next_monday, datetime.min.time(), local.tzinfo).replace(hour=hour)
        Goals(store).save(identifier(key), {"title": title, "prompt": prompt, "frequency_minutes": minutes,
                          "zone": ZONE, "scheduled_at": due.isoformat(), "watch_provider": provider},
                          request=prompt, check_in=key == "card")


def snapshot(store):
    with store.connect() as db:
        result = {}
        for row in db.execute("SELECT id,title,prompt,zone,status,revision,interval_minutes,watch_provider,"
                              "scheduled_at,next_run,pdf_instruction FROM goals ORDER BY id"):
            item = dict(row)
            key = item.pop("id")
            work = assessment(db, key)
            followup = for_goal(db, key)
            item["check_in"] = bool(work and work["required"])
            item["check_in_due"] = followup["due"] if followup else None
            result[key] = item
        return result


def only_model_socket(path):
    def restrict(event, args):
        if event == "socket.connect" and (args[0].family != socket.AF_UNIX or args[1] != str(path)):
            raise PermissionError("The audit allows only the supplied local model socket.")
        if event in {"subprocess.Popen", "os.system", "os.posix_spawn", "socket.getaddrinfo"}:
            raise PermissionError("External processes and address resolution are disabled during the audit.")
    sys.addaudithook(restrict)


class LocalModel:
    def __init__(self, path):
        self.path = path
        self.calls = 0

    async def complete(self, messages, limit):
        dialogue = Dialogue(None, self.path, None)
        try:
            return await dialogue.complete(dialogue.local, messages, limit, temperature=0)
        finally:
            await dialogue.close()

    def generate(self, messages, limit):
        self.calls += 1
        return asyncio.run(self.complete(messages, limit))

    def chat(self, messages, config, language, limit, on_speech, **options):
        return self.generate(messages, limit)


class Mailbox:
    def __init__(self, mode):
        self.mode = mode
        self.reads = []

    def read(self, root, intent, query, zone, started):
        read = {"provider": intent.provider, "query": query}
        self.reads.append(read)
        if intent.provider != "gmail":
            return intent.provider, [], "No fixture for this provider."

        def list_messages(url, token, params):
            read["provider_query"] = params["q"]
            return {"messages": [{"id": key} for key in MAIL] if self.mode == "mixed" else []}

        with patch("nex.accounts.read.Credentials") as vault, patch("nex.accounts.google.get", list_messages), patch(
            "nex.accounts.gmail_watch.message", side_effect=lambda token, key: (0, MAIL[key]),
        ):
            vault.return_value.status.return_value = {"gmail": "reconnect" if self.mode == "outage" else "connected"}
            vault.return_value.token.return_value = "fixture"
            return prepare_account(root, intent, query, zone, started)
