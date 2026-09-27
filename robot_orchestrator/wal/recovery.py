import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from robot_orchestrator.state import store
from robot_orchestrator.state.store import Database
from robot_orchestrator.wal.journal import Journal, JournalRow

CommittingHandler = Callable[[Database, JournalRow, Journal], str]


@dataclass
class RecoveryReport:
    rolled_back: list[str] = field(default_factory=list)
    rolled_forward: list[str] = field(default_factory=list)
    abandoned: list[str] = field(default_factory=list)


def _cleanup_staging_paths(payload: dict) -> None:
    for p in payload.get("staging_paths", []):
        path = Path(p)
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        elif path.exists():
            path.unlink(missing_ok=True)


def default_committing_handler(db: Database, row: JournalRow, journal: Journal) -> str:
    _cleanup_staging_paths(row.payload)
    journal.abandoned(row.op_id, error="no committing handler registered for this op_type; abandoned defensively")
    return "abandoned"


def firmware_flash_committing_handler(db: Database, row: JournalRow, journal: Journal) -> str:
    target = row.payload.get("target", row.subject)

    def mark_dirty(conn):
        store.upsert_flash_state(conn, target, dirty=1, flashed_sha=None, verified=0)

    journal.abandoned(row.op_id, error="power loss during flash; MCU content unknown", state_update=mark_dirty)
    return "abandoned"


DEFAULT_COMMITTING_HANDLERS: dict[str, CommittingHandler] = {
    "firmware_flash": firmware_flash_committing_handler,
}


def run(
    db: Database,
    journal: Journal,
    committing_handlers: dict[str, CommittingHandler] | None = None,
) -> RecoveryReport:
    handlers = {**DEFAULT_COMMITTING_HANDLERS, **(committing_handlers or {})}
    report = RecoveryReport()
    for row in journal.active_ops():
        if row.phase in ("intent", "prepared"):
            _cleanup_staging_paths(row.payload)
            journal.rolled_back(row.op_id, error=f"recovered from {row.phase} on startup")
            report.rolled_back.append(row.op_id)
        elif row.phase == "committing":
            handler = handlers.get(row.op_type, default_committing_handler)
            outcome = handler(db, row, journal)
            if outcome == "committed":
                report.rolled_forward.append(row.op_id)
            elif outcome == "rolled_back":
                report.rolled_back.append(row.op_id)
            else:
                report.abandoned.append(row.op_id)
    return report
