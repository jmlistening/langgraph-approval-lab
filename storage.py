import sqlite3
from contextlib import closing
from pathlib import Path


class ApprovalError(Exception):
    """An invalid ticket or approval operation."""


class TicketStore:
    def __init__(self, path: Path):
        self.path = path
        with closing(self.connect()) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS tickets (
                    id TEXT PRIMARY KEY,
                    revision INTEGER NOT NULL DEFAULT 1,
                    draft TEXT NOT NULL,
                    applied_text TEXT
                );
                CREATE TABLE IF NOT EXISTS applications (
                    run_id TEXT PRIMARY KEY,
                    ticket_id TEXT NOT NULL REFERENCES tickets(id),
                    revision INTEGER NOT NULL,
                    approved_text TEXT NOT NULL,
                    UNIQUE(ticket_id, revision)
                );
            """)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        return db

    def create(self, ticket_id: str, text: str):
        with closing(self.connect()) as db, db:
            result = db.execute(
                "INSERT INTO tickets (id, draft) VALUES (?, ?) ON CONFLICT(id) DO NOTHING",
                (ticket_id, text),
            )
            if not result.rowcount:
                raise ApprovalError(f"Ticket already exists: {ticket_id}")
            return self._get(db, ticket_id)

    def revise(self, ticket_id: str, text: str):
        with closing(self.connect()) as db, db:
            result = db.execute(
                "UPDATE tickets SET revision = revision + 1, draft = ? WHERE id = ?",
                (text, ticket_id),
            )
            if not result.rowcount:
                raise ApprovalError(f"Unknown ticket: {ticket_id}")
            return self._get(db, ticket_id)

    def get(self, ticket_id: str):
        with closing(self.connect()) as db:
            return self._get(db, ticket_id)

    @staticmethod
    def _get(db, ticket_id):
        row = db.execute("""
            SELECT tickets.*,
                   (SELECT COUNT(*) FROM applications WHERE ticket_id = tickets.id)
                       AS application_count
            FROM tickets WHERE id = ?
        """, (ticket_id,)).fetchone()
        if row is None:
            raise ApprovalError(f"Unknown ticket: {ticket_id}")
        return dict(row)

    def apply(self, run_id: str, ticket_id: str, revision: int, approved_text: str) -> str:
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute(
                "SELECT ticket_id, revision, approved_text FROM applications WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if previous is not None:
                if tuple(previous) != (ticket_id, revision, approved_text):
                    raise ApprovalError(f"Run ID already belongs to another application: {run_id}")
                return "applied"

            ticket = self._get(db, ticket_id)
            if ticket["revision"] != revision or ticket["draft"] != approved_text:
                return "stale"

            result = db.execute("""
                INSERT INTO applications (run_id, ticket_id, revision, approved_text)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(ticket_id, revision) DO NOTHING
            """, (run_id, ticket_id, revision, approved_text))
            if not result.rowcount:
                return "already_applied"

            db.execute(
                "UPDATE tickets SET applied_text = ? WHERE id = ?",
                (approved_text, ticket_id),
            )
            return "applied"
