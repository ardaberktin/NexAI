"""Retrieve earlier exchanges from the unlocked workspace's shared Chat timeline."""

import json
import logging
import sqlite3

from ..conversation_history import SHARED_QUESTION, TURN_ID, time_key
from ..passages import matching_passage
from ..store import CURRENT_EVIDENCE, search_prefixes
from .keys import validate_session

LOG = logging.getLogger(__name__)
TOPIC_LIMIT = 4
RECENT_LIMIT = 8

PAIRS = f"""
    JOIN memory m ON m.id='a-'||{TURN_ID} AND m.kind='answer'
    WHERE {SHARED_QUESTION} AND {CURRENT_EVIDENCE}
      AND q.started<:before AND m.started<:before
      AND q.id NOT IN (SELECT value FROM json_each(:exclude))
"""
PAIR_FIELDS = "q.id,q.started,q.body AS question,m.body AS answer"
RECENT = f"""
    SELECT {PAIR_FIELDS} FROM memory q {PAIRS}
    ORDER BY q.started DESC,q.number DESC LIMIT :limit
"""
BY_TOPIC = f"""
    SELECT {PAIR_FIELDS} FROM memory_fts
    JOIN memory hit ON hit.number=memory_fts.rowid
    JOIN memory q ON q.id=CASE WHEN hit.kind='answer' THEN 'q-'||substr(hit.id,3) ELSE hit.id END
                   OR (hit.kind='answer' AND q.kind='recording' AND q.id=substr(hit.id,3))
    {PAIRS}
      AND memory_fts MATCH :match AND hit.kind IN ('question','answer','recording')
    ORDER BY bm25(memory_fts),q.started DESC,q.number DESC LIMIT :limit
"""


def recall_chat(store, turn, session, query):
    with store.privacy.transaction() as db:
        validate_session(db, turn.device, session)
    excluded = {"q-" + turn.id}
    excluded.update("q-" + item["exchange_id"] for item in turn.history if "exchange_id" in item)
    params = {"before": time_key(turn.started), "exclude": json.dumps(sorted(excluded))}
    terms = search_prefixes(query)
    try:
        with store.connect() as db:
            if terms:
                # Both halves of a pair can match. Fetch enough hits to deduplicate them.
                match = " OR ".join(f'"{term}"*' for term in terms)
                rows = db.execute(BY_TOPIC, params | {"match": match, "limit": TOPIC_LIMIT * 2})
                pairs = {}
                for row in rows:
                    pairs.setdefault(row["id"], row)
                exchanges = excerpts(list(pairs.values())[:TOPIC_LIMIT], query, 360)
            else:
                rows = db.execute(RECENT, params | {"limit": RECENT_LIMIT})
                exchanges = excerpts(rows, "", 140)
    except sqlite3.Error as error:
        LOG.warning("Chat history recall failed (%s)", type(error).__name__)
        return {"status": "unavailable", "exchanges": []}
    return {"status": "found" if exchanges else "no_match", "exchanges": exchanges}


def excerpts(rows, query, limit):
    return [
        {"date": row["started"], "messages": [
            {"role": "user", "text": matching_passage(row["question"], query, limit)},
            {"role": "assistant", "text": matching_passage(row["answer"], query, limit)},
        ]}
        for row in rows
    ]
