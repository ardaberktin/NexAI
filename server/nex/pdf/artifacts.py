"""Store only verified PDFs and read their metadata without loading file bytes."""

from ..workspace_schema import WorkspaceMissing


def receipt(row):
    return {"status": "saved", "id": row["id"], "title": row["title"], "pages": row["pages"],
            "goal_id": row["goal_id"]}


def existing(db, identifier):
    return db.execute("SELECT id, goal_id, title, pages FROM pdf_artifacts WHERE id=?", (identifier,)).fetchone()


def insert(db, identifier, goal_id, date, document):
    db.execute("INSERT INTO pdf_artifacts VALUES(?,?,?,?,?,?)",
               (identifier, goal_id, date, document.title, document.pages, document.data))


def files(db, goal):
    where, params = ("WHERE p.goal_id=?", (goal,)) if goal else ("", ())
    rows = db.execute(
        f"""SELECT p.id,p.goal_id,p.created,p.title,p.pages,g.title AS goal,a.detail
            FROM pdf_artifacts p
            LEFT JOIN goals g ON g.id=p.goal_id
            LEFT JOIN goal_activity a ON a.id=p.id
            {where} ORDER BY p.created DESC,p.id DESC LIMIT 50""", params,
    )
    return [
        {"id": row["id"], "title": row["title"], "date": row["created"], "kind": "pdf",
         "subtitle": row["goal"] or "PDF", "text": row["detail"] or "", "goal_id": row["goal_id"],
         "page_count": row["pages"]}
        for row in rows
    ]


def content(store, identifier):
    with store.connect() as db:
        row = db.execute("SELECT pdf FROM pdf_artifacts WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise WorkspaceMissing("That PDF is unavailable.")
        return row["pdf"]
