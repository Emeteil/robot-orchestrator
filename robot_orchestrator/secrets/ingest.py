from robot_orchestrator.paths import Paths
from robot_orchestrator.secrets import store
from robot_orchestrator.secrets.protocol import SecretsPayload
from robot_orchestrator.wal.journal import Journal


def ingest_payload(paths: Paths, journal: Journal, payload: SecretsPayload) -> None:
    op_id = journal.begin("secrets_write", "secrets", {})
    try:
        journal.prepared(op_id)
        journal.committing(op_id)
        store.write_secrets(paths, payload)
    except Exception as e:
        journal.rolled_back(op_id, error=str(e))
        raise
    journal.committed(op_id)
