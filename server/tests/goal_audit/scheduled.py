"""Drive the real goal worker at its recorded due times using a synthetic Gmail transport."""

import argparse
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fixtures import MAIL, ZONE, LocalModel, identifier, only_model_socket

from nex.goal_checks import fingerprint
from nex.goals import Goals
from nex.store import Store

PROMPT = "Notify me about new paid, remote software engineering job postings. Exclude on-site and unpaid roles."
JOBS = MAIL | {
    "opening": MAIL["opening"] | {"body": "Northstar is hiring a salaried, fully remote software engineer. Apply by December 4."},
    "onsite": {"sender": "Beacon Staffing", "subject": "Software engineer hiring",
               "body": "Paid full-time engineering job based on-site five days a week. No remote option."},
    "unpaid": {"sender": "Campus Careers", "subject": "Remote software engineering internship",
               "body": "Applications are open for an unpaid, remote engineering role."},
    "second": {"sender": "Cedar Labs", "subject": "Distributed systems team is hiring",
               "body": "Salaried software engineer. Work from anywhere. Applications close December 8."},
}
POLLS = (
    ("S1", ["opening", "onsite", "unpaid", "receipt"], True, ["opening"]),
    ("S2", ["opening", "onsite", "unpaid", "receipt"], True, []),
    ("S3", ["second"], False, []),
    ("S4", ["second", "event"], True, ["second"]),
)


def poll(goals, model, goal_id, arrived, case, messages, connected, expected):
    before = goals.detail(goal_id)
    due = datetime.fromisoformat(before["next_run"])
    for name in messages:
        arrived.setdefault(name, (int((due - timedelta(seconds=1)).timestamp() * 1000), JOBS[name]))
    with patch("nex.goal_checks.Credentials") as credentials, patch(
        "nex.goal_checks.gmail_watch.message_ids", return_value=messages,
    ), patch("nex.goal_checks.gmail_watch.message", side_effect=lambda token, key: arrived[key]) as fetched:
        credentials.return_value.status.return_value = {"gmail": "connected" if connected else "disconnected"}
        credentials.return_value.token.return_value = "fixture"
        start_calls = model.calls
        goals.run(due)
    after = goals.detail(goal_id)
    with goals.store.connect() as db:
        matched = {row[0] for row in db.execute("SELECT fingerprint FROM goal_seen WHERE goal_id=?", (goal_id,))}
    before_ids = {entry["id"] for entry in before["activity"]}
    updates = [entry["detail"] for entry in after["activity"] if entry["artifact"] and entry["id"] not in before_ids]
    newly_matched = sorted(entry.args[1] for entry in fetched.call_args_list if fingerprint(entry.args[1]) in matched)
    return {"id": case, "prompt": PROMPT, "input_ids": messages, "connected": connected,
            "expected_matches": expected, "fetched": [entry.args[1] for entry in fetched.call_args_list],
            "matches": newly_matched, "updates": updates, "error": after["error"], "due": before["next_run"],
            "next_run": after["next_run"], "last_checked": after["last_checked"], "model_calls": model.calls - start_calls}


def evaluate_polls(model):
    with TemporaryDirectory() as root:
        goals = Goals(Store(root, 0, 2**20), classify=lambda messages: model.generate(messages, 8),
                      generate=model.generate)
        goal_id = identifier("scheduled")
        due = datetime.now(UTC) + timedelta(hours=1)
        goals.save(goal_id, {"title": "Remote job leads", "prompt": PROMPT, "zone": ZONE,
                             "watch_provider": "gmail", "frequency_minutes": 60, "scheduled_at": due.isoformat()},
                   request=PROMPT, language="en")
        arrived = {}
        for inputs in POLLS:
            yield poll(goals, model, goal_id, arrived, *inputs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("socket", type=Path)
    args = parser.parse_args()
    if not args.socket.is_socket():
        parser.error("Supply the Unix socket of the disposable model server.")
    only_model_socket(args.socket)
    model = LocalModel(args.socket)
    for result in evaluate_polls(model):
        print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
