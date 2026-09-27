import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from typing import Callable

from robot_orchestrator.state.store import Database

ACTIVE_PHASES = ("intent", "prepared", "committing")


class OperationInProgress(Exception):
    pass


@dataclass
class JournalRow:
    op_id: str
    op_type: str
    subject: str
    phase: str
    payload: dict
    boot_id: str
    created_at: float
    updated_at: float
    error: str | None


def _row_to_journal(row: sqlite3.Row) -> JournalRow:
    return JournalRow(
        op_id=row["op_id"],
        op_type=row["op_type"],
        subject=row["subject"],
        phase=row["phase"],
        payload=json.loads(row["payload"]),
        boot_id=row["boot_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        error=row["error"],
    )


class Journal:
    def __init__(self, db: Database, boot_id: str):
        self.db = db
        self.boot_id = boot_id

    def active_for_subject(self, subject: str) -> JournalRow | None:
        row = self.db.conn.execute(
            "SELECT * FROM journal WHERE subject = ? AND phase IN ('intent','prepared','committing')",
            (subject,),
        ).fetchone()
        return _row_to_journal(row) if row else None

    def get(self, op_id: str) -> JournalRow | None:
        row = self.db.conn.execute("SELECT * FROM journal WHERE op_id = ?", (op_id,)).fetchone()
        return _row_to_journal(row) if row else None

    def active_ops(self) -> list[JournalRow]:
        rows = self.db.conn.execute(
            "SELECT * FROM journal WHERE phase IN ('intent','prepared','committing') ORDER BY created_at"
        ).fetchall()
        return [_row_to_journal(r) for r in rows]

    def begin(self, op_type: str, subject: str, payload: dict, op_id: str | None = None) -> str:
        op_id = op_id or uuid.uuid4().hex
        now = time.time()
        try:
            with self.db.transaction() as conn:
                conn.execute(
                    "INSERT INTO journal "
                    "(op_id, op_type, subject, phase, payload, boot_id, created_at, updated_at, error) "
                    "VALUES (?, ?, ?, 'intent', ?, ?, ?, ?, NULL)",
                    (op_id, op_type, subject, json.dumps(payload), self.boot_id, now, now),
                )
        except sqlite3.IntegrityError as e:
            raise OperationInProgress(f"{subject!r} already has an active operation") from e
        return op_id

    def _set_phase(
        self,
        op_id: str,
        phase: str,
        error: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        c = conn or self.db.conn
        c.execute(
            "UPDATE journal SET phase = ?, updated_at = ?, error = ? WHERE op_id = ?",
            (phase, time.time(), error, op_id),
        )

    def prepared(self, op_id: str) -> None:
        self._set_phase(op_id, "prepared")

    def committing(self, op_id: str) -> None:
        self._set_phase(op_id, "committing")

    def committed(
        self,
        op_id: str,
        state_update: Callable[[sqlite3.Connection], None] | None = None,
    ) -> None:
        with self.db.transaction() as conn:
            self._set_phase(op_id, "committed", conn=conn)
            if state_update is not None:
                state_update(conn)

    def rolled_back(
        self,
        op_id: str,
        error: str | None = None,
        state_update: Callable[[sqlite3.Connection], None] | None = None,
    ) -> None:
        with self.db.transaction() as conn:
            self._set_phase(op_id, "rolled_back", error=error, conn=conn)
            if state_update is not None:
                state_update(conn)

    def abandoned(
        self,
        op_id: str,
        error: str | None = None,
        state_update: Callable[[sqlite3.Connection], None] | None = None,
    ) -> None:
        with self.db.transaction() as conn:
            self._set_phase(op_id, "abandoned", error=error, conn=conn)
            if state_update is not None:
                state_update(conn)
