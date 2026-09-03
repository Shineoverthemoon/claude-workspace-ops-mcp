"""SQLite connection and schema.

One file holds all durable state so the whole system can be inspected, backed
up, or thrown away as a unit. Timestamps are stored as UTC ISO-8601 strings.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS proposals (
    id             TEXT PRIMARY KEY,
    created_at     TEXT NOT NULL,
    kind           TEXT NOT NULL,
    root_folder_id TEXT NOT NULL,
    plan_hash      TEXT NOT NULL,
    status         TEXT NOT NULL,
    payload        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS approvals (
    id          TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL,
    plan_hash   TEXT NOT NULL,
    approver    TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    consumed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_approvals_proposal ON approvals(proposal_id);

CREATE TABLE IF NOT EXISTS grants (
    file_id    TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    mime_type  TEXT NOT NULL,
    source     TEXT NOT NULL,
    granted_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ts             TEXT NOT NULL,
    correlation_id TEXT NOT NULL,
    actor          TEXT NOT NULL,
    tool           TEXT NOT NULL,
    action         TEXT NOT NULL,
    outcome        TEXT NOT NULL,
    proposal_id    TEXT,
    reason_code    TEXT,
    approved_by    TEXT,
    detail         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_proposal ON audit(proposal_id);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit(ts);
"""


def to_iso(value: datetime) -> str:
    """UTC ISO-8601, fixed width.

    Microseconds are always emitted so stored timestamps sort lexicographically
    in the same order as chronologically - approval expiry is enforced by a SQL
    string comparison, and a variable-width format would make that subtly wrong.
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def from_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


class Database:
    """Thin SQLite wrapper. Not a general-purpose ORM by design."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        if self.path.parent != Path(""):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self.path), check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def _migrate(self) -> None:
        self._conn.executescript(SCHEMA)
        self._conn.execute(
            "INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('version', ?)",
            (str(SCHEMA_VERSION),),
        )

    @property
    def connection(self) -> sqlite3.Connection:
        return self._conn

    def execute(self, sql: str, params: tuple[object, ...] = ()) -> sqlite3.Cursor:
        return self._conn.execute(sql, params)

    def query(self, sql: str, params: tuple[object, ...] = ()) -> list[sqlite3.Row]:
        return list(self._conn.execute(sql, params).fetchall())

    def query_one(self, sql: str, params: tuple[object, ...] = ()) -> sqlite3.Row | None:
        return self._conn.execute(sql, params).fetchone()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")

    def close(self) -> None:
        self._conn.close()
