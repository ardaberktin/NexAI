"""Short-lived, device- and unlock-bound context for the planning model."""
import json
from dataclasses import dataclass
from time import monotonic

from ..conversation_history import RESUME_WINDOW
from ..store import excerpt
from .keys import validate_session
from .state import digest


@dataclass
class Exchange:
    session: str
    expires: float
    messages: tuple


class ChatFollowups:
    def __init__(self):
        self.latest = {}

    def previous(self, device, token):
        item = self.latest.get(device)
        if token and item and item.expires > monotonic() and item.session == digest(token):
            return item.messages
        return ()

    def clear(self, device):
        self.latest.pop(device, None)

    def remember(self, device, token, identifier, question, result):
        previous = self.previous(device, token)
        if previous and previous[-1].get("private"):
            previous = ()
        self.save(device, token, previous, identifier, question, result["text"], engine=result["engine"])

    def remember_private(self, state, device, token, identifier, question, tools):
        if not token:
            self.clear(device)
            return
        with state.transaction() as db:
            validate_session(db, device, token)
            row = db.execute("SELECT answer, sources, cursor, goal_id, choices FROM requests LEFT JOIN citations "
                             "ON requests.id=citations.request_id LEFT JOIN request_recall "
                             "ON requests.id=request_recall.request_id LEFT JOIN request_goals "
                             "ON requests.id=request_goals.request_id LEFT JOIN request_goal_choices "
                             "ON requests.id=request_goal_choices.request_id WHERE requests.id=? "
                             "AND device=? AND state='ready'",
                             (identifier, device)).fetchone()
        if row:
            selected = json.dumps(list(tools), ensure_ascii=False)
            sources = json.loads(row["sources"] or "[]")
            self.save(device, token, self.previous(device, token), identifier, question, row["answer"], private=True,
                      selected=selected, sources=sources, selected_tools=[tool["name"] for tool in tools],
                      goal_id=row["goal_id"],
                      goal_choices=json.loads(row["choices"]) if row["choices"] else None,
                      recall_cursor=json.loads(row["cursor"]) if row["cursor"] else None,
                      engine="calculation" if any(t["name"] == "age" for t in tools) else "private_request")

    def save(self, device, token, previous, identifier, question, response, **metadata):
        self.clear(device)
        if token:
            messages = (*previous[-6:], {"kind": "question", "text": excerpt(question, 900), "exchange_id": identifier},
                        {"kind": "answer", "text": excerpt(response, 900), **metadata})
            self.latest[device] = Exchange(digest(token), monotonic() + RESUME_WINDOW.total_seconds(), messages)
