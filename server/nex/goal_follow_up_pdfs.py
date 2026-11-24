"""Attach PDFs to delivered check-ins without blocking their scheduled questions."""

import json
import logging
from datetime import UTC, datetime, timedelta

from .feed import stamp
from .pdf import artifacts
from .pdf.document import PDFError, plain_text
from .pdf.scheduled import render_pdf
from .workspace_features import revision as feature_revision

LOG = logging.getLogger(__name__)
DUE = """
    SELECT f.id,f.goal_id,f.question,f.choices,f.pdf_instruction,f.pdf_retry_at,g.title,g.reply_language
    FROM goal_follow_ups f JOIN goals g ON g.id=f.goal_id
    WHERE f.pdf_retry_at<=?
"""


def deliver_pdfs(store, generate, anchor=None):
    anchor = anchor or datetime.now(UTC)
    now = stamp(anchor)
    with store.connect() as db:
        epoch = feature_revision(db, "goals")
        if epoch is None:
            return
        due = db.execute(DUE + " ORDER BY f.pdf_retry_at,f.id LIMIT 4", (now,)).fetchall()
    for row in due:
        try:
            pdf = check_in_pdf(row, generate)
        except PDFError as error:
            LOG.warning("Nex follow-up PDF will retry (%s)", error.code)
            pdf = None
        with store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if feature_revision(db, "goals") != epoch:
                return
            pending = db.execute("SELECT pdf_retry_at FROM goal_follow_ups WHERE id=?", (row["id"],)).fetchone()
            if pending is None or pending["pdf_retry_at"] != row["pdf_retry_at"]:
                continue
            retry = stamp(anchor + timedelta(minutes=5)) if pdf is None else None
            if pdf is not None:
                artifacts.insert(db, row["id"], row["goal_id"], now, pdf)
            db.execute("UPDATE goal_follow_ups SET pdf_retry_at=? WHERE id=?", (retry, row["id"]))


def check_in_pdf(row, generate):
    contents = [row["question"], *json.loads(row["choices"])]
    context = {"goal_title": row["title"], "language": row["reply_language"],
               "document_brief": row["pdf_instruction"], "published_check_in": contents}
    expected = {plain_text(text, 2500) for text in contents}
    return render_pdf(generate, context, lambda draft: expected.issubset(draft.splitlines()))
