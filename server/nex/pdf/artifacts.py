"""Store only verified PDFs and read their metadata without loading file bytes."""

from uuid import NAMESPACE_URL, uuid5

from ..workspace_schema import WorkspaceMissing


def request_id(device, identifier):
    return str(uuid5(NAMESPACE_URL, f"nex:pdf:{device}:{identifier}"))


def receipt(row):
    return {"status": "saved", "id": row["id"], "title": row["title"], "pages": row["pages"],
            "goal_id": row["goal_id"]}


def existing(db, identifier):
    return db.execute("SELECT id, goal_id, title, pages FROM pdf_artifacts WHERE id=?", (identifier,)).fetchone()


def insert(db, identifier, goal_id, date, document):
    db.execute("INSERT INTO pdf_artifacts VALUES(?,?,?,?,?,?)",
               (identifier, goal_id, date, document.title, document.pages, document.data))


def file_item(row, detail=""):
    return {"id": row["id"], "title": row["title"], "date": row["created"], "kind": "pdf",
            "subtitle": row["goal"] or "PDF", "text": detail, "goal_id": row["goal_id"],
            "page_count": row["pages"]}


def by_ids(db, identifiers):
    if not identifiers:
        return {}
    placeholders = ", ".join("?" for _ in identifiers)
    rows = db.execute(
        f"""SELECT p.id,p.goal_id,p.created,p.title,p.pages,g.title AS goal
            FROM pdf_artifacts p LEFT JOIN goals g ON g.id=p.goal_id
            WHERE p.id IN ({placeholders})""", tuple(identifiers),
    )
    return {row["id"]: file_item(row) for row in rows}


def files(db, goal):
    where, params = ("WHERE p.goal_id=?", (goal,)) if goal else ("", ())
    rows = db.execute(
        f"""SELECT p.id,p.goal_id,p.created,p.title,p.pages,g.title AS goal,a.detail
            FROM pdf_artifacts p
            LEFT JOIN goals g ON g.id=p.goal_id
            LEFT JOIN goal_activity a ON a.id=p.id
            {where} ORDER BY p.created DESC,p.id DESC LIMIT 50""", params,
    )
    return [file_item(row, row["detail"] or "") for row in rows]


def content(store, identifier):
    with store.connect() as db:
        row = db.execute("SELECT pdf FROM pdf_artifacts WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise WorkspaceMissing("That PDF is unavailable.")
        return row["pdf"]
