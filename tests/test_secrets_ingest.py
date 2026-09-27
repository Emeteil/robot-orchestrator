from pathlib import Path

import pytest

from robot_orchestrator.paths import Paths
from robot_orchestrator.secrets import ingest, store
from robot_orchestrator.secrets.protocol import SecretsPayload
from robot_orchestrator.state.store import Database
from robot_orchestrator.wal.journal import Journal


@pytest.fixture
def env(tmp_path: Path):
    paths = Paths(tmp_path / "state", tmp_path / "logs", tmp_path / "run")
    paths.ensure()
    db = Database(paths.state_db)
    journal = Journal(db, boot_id="boot-1")
    yield paths, db, journal
    db.close()


def test_ingest_payload_writes_secrets_and_commits_journal(env):
    paths, db, journal = env
    payload = SecretsPayload(v=1, issued_at="2026-09-27T12:00:00Z", secrets={"GEMINI_API_KEY": "abc"})

    ingest.ingest_payload(paths, journal, payload)

    loaded = store.load_secrets(paths)
    assert loaded["secrets"] == {"GEMINI_API_KEY": "abc"}

    rows = [row for row in journal.active_ops()]
    assert rows == []

    row = db.conn.execute("SELECT * FROM journal WHERE op_type = 'secrets_write'").fetchone()
    assert row["phase"] == "committed"


def test_ingest_payload_rolls_back_and_reraises_on_write_failure(env, monkeypatch):
    paths, db, journal = env
    payload = SecretsPayload(v=1, issued_at="2026-09-27T12:00:00Z", secrets={"GEMINI_API_KEY": "abc"})

    def _boom(paths_arg, payload_arg):
        raise RuntimeError("disk full")

    monkeypatch.setattr(ingest.store, "write_secrets", _boom)

    with pytest.raises(RuntimeError):
        ingest.ingest_payload(paths, journal, payload)

    row = db.conn.execute("SELECT * FROM journal WHERE op_type = 'secrets_write'").fetchone()
    assert row["phase"] == "rolled_back"
    assert row["error"] == "disk full"
