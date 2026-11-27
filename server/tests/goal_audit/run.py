"""Run from server/ with PYTHONPATH=.:tests and a disposable llama-server socket."""

import argparse
import inspect
import json
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from types import SimpleNamespace
from unittest.mock import patch

from cases import SCENARIOS
from fixtures import (
    ZONE,
    LocalModel,
    Mailbox,
    identifier,
    only_model_socket,
    seed,
    snapshot,
)

from conversation_fixtures import unlock
from nex.goal_follow_up_jobs import GoalFollowUpJobs
from nex.goal_follow_up_notices import GoalFollowUpNotices
from nex.goals import Goals
from nex.privacy import public
from nex.privacy.chat_history import ChatFollowups
from nex.privacy.requests import inbox
from nex.store import Store


def changed(before, after):
    changes = {}
    for key in sorted(before.keys() | after.keys()):
        if key not in after:
            changes[key] = {"deleted": before[key]}
        elif key not in before:
            changes[key] = {"created": after[key]}
        elif before[key] != after[key]:
            changes[key] = {field: [value, after[key][field]] for field, value in before[key].items()
                            if after[key][field] != value}
    return changes


@contextmanager
def observe_plan():
    selected = {}
    original = public.plan_chat

    def observe(*args, **kwargs):
        plan, private = original(*args, **kwargs)
        selected.update(tools=list(plan.tools), error=plan.error, private=private)
        return plan, private

    with patch.object(public, "plan_chat", observe):
        yield selected


def follow_up(store, model, goal_id, request_id):
    jobs = GoalFollowUpJobs(store, model.generate)
    attempts = []
    for _ in range(4):
        with store.connect() as db:
            work = db.execute("SELECT next_try FROM goal_follow_up_work WHERE goal_id=?", (goal_id,)).fetchone()
        if not work or not work[0]:
            break
        attempts.append(work[0])
        jobs.decide(datetime.fromisoformat(work[0]))
    goals = Goals(store)
    saved = goals.detail(goal_id)["follow_up"]
    if saved:
        anchor = datetime.fromisoformat(attempts[-1]) if attempts else datetime.now(UTC)
        GoalFollowUpNotices(store, model.generate).run(anchor)
    item = next((row for row in inbox(store.privacy)["items"] if row["id"] == request_id), {})
    if saved:
        jobs.deliver(datetime.fromisoformat(saved["due"]))
        saved = goals.detail(goal_id)["follow_up"]
    return {"attempts": attempts, "card": saved, "chat_state": item.get("state"), "chat_reply": item.get("answer")}


def evaluate(model, scenario, *, check_follow_up=False):
    with TemporaryDirectory() as root:
        store = Store(root, 0, 2**20)
        seed(store, scenario.seeds)
        history = ChatFollowups()
        mailbox = Mailbox(scenario.mail)
        assistant = SimpleNamespace(store=store, settings=None, generate_conversation=model.chat)
        for turn in scenario.turns:
            request_id = identifier(turn.key)
            before = snapshot(store)
            start, start_time = monotonic(), datetime.now(UTC).isoformat(timespec="seconds")
            calls = model.calls
            mailbox.reads.clear()
            options = {"agent_loop": True} if "agent_loop" in inspect.signature(public.chat).parameters else {}
            with patch("nex.conversation.tools.prepare_account", side_effect=mailbox.read), observe_plan() as plan:
                result = public.chat(assistant, request_id, "mac", turn.prompt, "en", ZONE,
                                     session=unlock(store), followups=history, **options)
            item = next((row for row in inbox(store.privacy)["items"] if row["id"] == request_id), {})
            row = {"id": turn.key, "prompt": turn.prompt, "expected": turn.expected, "seeds": scenario.seeds,
                   "mail": scenario.mail, "started": start_time, "seconds": round(monotonic() - start, 2),
                   "model_calls": model.calls - calls, "plan": plan, "reads": list(mailbox.reads),
                   "status": item.get("state", result["engine"]), "reply": item.get("answer", result.get("text")),
                   "goal_id": item.get("goal_id") or result.get("goal_id"), "changes": changed(before, snapshot(store))}
            if check_follow_up and row["goal_id"]:
                row["deferred_follow_up"] = follow_up(store, model, row["goal_id"], request_id)
            yield row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("socket", type=Path)
    parser.add_argument("cases", nargs="*", help="First turn IDs of the conversations to run (default: all)")
    parser.add_argument("--follow-up-worker", action="store_true", help="Run the check-in worker after a saved turn")
    args = parser.parse_args()
    if not args.socket.is_socket():
        parser.error("Supply the Unix socket of the disposable model server.")
    only_model_socket(args.socket)
    model = LocalModel(args.socket)
    for scenario in SCENARIOS:
        if not args.cases or scenario.turns[0].key in args.cases:
            for row in evaluate(model, scenario, check_follow_up=args.follow_up_worker):
                print(json.dumps(row, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
