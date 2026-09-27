import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS journal (
    op_id TEXT PRIMARY KEY,
    op_type TEXT NOT NULL,
    subject TEXT NOT NULL,
    phase TEXT NOT NULL,
    payload TEXT NOT NULL,
    boot_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    error TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active ON journal(subject)
    WHERE phase IN ('intent', 'prepared', 'committing');

CREATE TABLE IF NOT EXISTS repo_state (
    repo TEXT PRIMARY KEY,
    current_release TEXT,
    current_sha TEXT,
    previous_release TEXT,
    previous_sha TEXT,
    bad_sha TEXT,
    last_fetch_at REAL,
    last_fetch_ok INTEGER,
    last_error TEXT,
    updated_at REAL
);

CREATE TABLE IF NOT EXISTS flash_state (
    target TEXT PRIMARY KEY,
    flashed_sha TEXT,
    branch TEXT,
    method TEXT,
    ci_run_id TEXT,
    image_sha256 TEXT,
    expected_build_date TEXT,
    expected_build_time TEXT,
    reported_build_date TEXT,
    reported_build_time TEXT,
    reported_ts_utc REAL,
    build_window_start_utc REAL,
    build_window_end_utc REAL,
    flash_started_at REAL,
    flash_finished_at REAL,
    verified INTEGER,
    dirty INTEGER,
    client_sha TEXT
);

CREATE TABLE IF NOT EXISTS flash_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target TEXT NOT NULL,
    recorded_at REAL NOT NULL,
    method TEXT,
    ci_run_id TEXT,
    image_sha256 TEXT,
    reported_build_date TEXT,
    reported_build_time TEXT,
    verified INTEGER,
    dirty INTEGER,
    error TEXT
);

CREATE TABLE IF NOT EXISTS boot_history (
    boot_id TEXT PRIMARY KEY,
    started_at REAL NOT NULL,
    gpio_forced INTEGER,
    mode TEXT,
    reasons TEXT,
    facts TEXT,
    final_state TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


class Transaction:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def __enter__(self) -> sqlite3.Connection:
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is None:
            self.conn.execute("COMMIT")
        else:
            self.conn.execute("ROLLBACK")
        return False


class Database:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(_SCHEMA)
        self.conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )

    def close(self) -> None:
        self.conn.close()

    def transaction(self) -> Transaction:
        return Transaction(self.conn)

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str, conn: sqlite3.Connection | None = None) -> None:
        c = conn or self.conn
        c.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def _upsert(conn: sqlite3.Connection, table: str, pk_col: str, pk_value: str, fields: dict) -> None:
    columns = [pk_col, *fields.keys()]
    values = [pk_value, *fields.values()]
    placeholders = ", ".join("?" for _ in columns)
    updates = ", ".join(f"{c} = excluded.{c}" for c in fields.keys())
    conn.execute(
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders}) "
        f"ON CONFLICT({pk_col}) DO UPDATE SET {updates}",
        values,
    )


def get_repo_state(conn: sqlite3.Connection, repo: str) -> dict | None:
    row = conn.execute("SELECT * FROM repo_state WHERE repo = ?", (repo,)).fetchone()
    return dict(row) if row else None


def upsert_repo_state(conn: sqlite3.Connection, repo: str, **fields) -> None:
    _upsert(conn, "repo_state", "repo", repo, fields)


def get_flash_state(conn: sqlite3.Connection, target: str) -> dict | None:
    row = conn.execute("SELECT * FROM flash_state WHERE target = ?", (target,)).fetchone()
    return dict(row) if row else None


def upsert_flash_state(conn: sqlite3.Connection, target: str, **fields) -> None:
    _upsert(conn, "flash_state", "target", target, fields)


def insert_flash_history(conn: sqlite3.Connection, target: str, recorded_at: float, **fields) -> None:
    columns = ["target", "recorded_at", *fields.keys()]
    values = [target, recorded_at, *fields.values()]
    placeholders = ", ".join("?" for _ in columns)
    conn.execute(f"INSERT INTO flash_history ({', '.join(columns)}) VALUES ({placeholders})", values)


def insert_boot_history(conn: sqlite3.Connection, boot_id: str, started_at: float, **fields) -> None:
    columns = ["boot_id", "started_at", *fields.keys()]
    values = [boot_id, started_at, *fields.values()]
    placeholders = ", ".join("?" for _ in columns)
    conn.execute(f"INSERT INTO boot_history ({', '.join(columns)}) VALUES ({placeholders})", values)


def update_boot_history(conn: sqlite3.Connection, boot_id: str, **fields) -> None:
    updates = ", ".join(f"{c} = ?" for c in fields.keys())
    conn.execute(f"UPDATE boot_history SET {updates} WHERE boot_id = ?", [*fields.values(), boot_id])
