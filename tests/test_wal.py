from pathlib import Path

import pytest

from robot_orchestrator.state import store
from robot_orchestrator.state.store import Database
from robot_orchestrator.wal import recovery
from robot_orchestrator.wal.journal import Journal, OperationInProgress


@pytest.fixture
def db(tmp_path: Path) -> Database:
    d = Database(tmp_path / "state.db")
    yield d
    d.close()


def test_journal_lifecycle_commits_state_in_one_transaction(db: Database):
    journal = Journal(db, boot_id="boot-1")
    op_id = journal.begin("repo_swap", "web-core", {"staging_paths": []})
    assert journal.get(op_id).phase == "intent"

    journal.prepared(op_id)
    journal.committing(op_id)

    def update(conn):
        store.upsert_repo_state(conn, "web-core", current_sha="deadbeef")

    journal.committed(op_id, state_update=update)

    assert journal.get(op_id).phase == "committed"
    assert store.get_repo_state(db.conn, "web-core")["current_sha"] == "deadbeef"


def test_only_one_active_op_per_subject(db: Database):
    journal = Journal(db, boot_id="boot-1")
    journal.begin("repo_swap", "web-core", {})
    with pytest.raises(OperationInProgress):
        journal.begin("repo_swap", "web-core", {})


def test_second_op_allowed_after_first_completes(db: Database):
    journal = Journal(db, boot_id="boot-1")
    op1 = journal.begin("repo_swap", "web-core", {})
    journal.committed(op1)
    op2 = journal.begin("repo_swap", "web-core", {})
    assert op2 != op1


def test_recovery_rolls_back_intent_and_cleans_staging(db: Database, tmp_path: Path):
    staging = tmp_path / "staging-dir"
    staging.mkdir()
    (staging / "partial.txt").write_text("half-written")

    journal = Journal(db, boot_id="boot-1")
    journal.begin("repo_swap", "web-core", {"staging_paths": [str(staging)]})

    report = recovery.run(db, journal)

    assert not staging.exists()
    assert len(report.rolled_back) == 1
    assert journal.active_ops() == []


def test_recovery_rolls_back_prepared(db: Database, tmp_path: Path):
    journal = Journal(db, boot_id="boot-1")
    op_id = journal.begin("repo_swap", "web-core", {"staging_paths": []})
    journal.prepared(op_id)

    report = recovery.run(db, journal)

    assert report.rolled_back == [op_id]
    assert journal.get(op_id).phase == "rolled_back"


def test_recovery_abandons_firmware_flash_interrupted_mid_committing(db: Database):
    journal = Journal(db, boot_id="boot-1")
    with db.transaction() as conn:
        store.upsert_flash_state(conn, "stm32", flashed_sha="old-sha", dirty=0, verified=1)

    op_id = journal.begin("firmware_flash", "stm32", {"target": "stm32"})
    journal.prepared(op_id)
    journal.committing(op_id)

    report = recovery.run(db, journal)

    assert report.abandoned == [op_id]
    flash_state = store.get_flash_state(db.conn, "stm32")
    assert flash_state["dirty"] == 1
    assert flash_state["flashed_sha"] is None
    assert journal.get(op_id).phase == "abandoned"


def test_recovery_generic_committing_op_type_is_abandoned_defensively(db: Database):
    journal = Journal(db, boot_id="boot-1")
    op_id = journal.begin("some_future_op_type", "thing", {"staging_paths": []})
    journal.prepared(op_id)
    journal.committing(op_id)

    report = recovery.run(db, journal)

    assert report.abandoned == [op_id]


def test_recovery_leaves_committed_ops_untouched(db: Database):
    journal = Journal(db, boot_id="boot-1")
    op_id = journal.begin("repo_swap", "web-core", {})
    journal.committed(op_id)

    report = recovery.run(db, journal)

    assert report.rolled_back == []
    assert report.abandoned == []
    assert journal.get(op_id).phase == "committed"


def test_recovery_is_idempotent_across_restarts(db: Database, tmp_path: Path):
    journal = Journal(db, boot_id="boot-1")
    journal.begin("repo_swap", "web-core", {"staging_paths": []})
    recovery.run(db, journal)

    journal2 = Journal(db, boot_id="boot-2")
    report2 = recovery.run(db, journal2)
    assert report2.rolled_back == []
    assert report2.abandoned == []
