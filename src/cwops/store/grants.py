"""Grant ledger - the server's own record of its drive.file access.

Google already refuses requests for files outside the drive.file grant. The
ledger is a second, local, deterministic check so the server never even attempts
a call it has no business making, and so "what can this agent reach?" is a
question answerable offline, by reading one table.
"""

from __future__ import annotations

from ..clock import Clock, SystemClock
from ..errors import NotGranted
from ..models import DriveFile, Grant, GrantSource
from .db import Database, from_iso, to_iso


class GrantLedger:
    def __init__(self, db: Database, clock: Clock | None = None) -> None:
        self._db = db
        self._clock = clock or SystemClock()

    def record(self, file: DriveFile, source: GrantSource) -> Grant:
        grant = Grant(
            file_id=file.id,
            name=file.name,
            mime_type=file.mime_type,
            source=source,
            granted_at=self._clock.now(),
        )
        self._db.execute(
            """
            INSERT INTO grants (file_id, name, mime_type, source, granted_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(file_id) DO UPDATE SET
                name = excluded.name,
                mime_type = excluded.mime_type,
                source = excluded.source
            """,
            (
                grant.file_id,
                grant.name,
                grant.mime_type,
                grant.source.value,
                to_iso(grant.granted_at),
            ),
        )
        return grant

    def record_many(self, files: list[DriveFile], source: GrantSource) -> int:
        for file in files:
            self.record(file, source)
        return len(files)

    def get(self, file_id: str) -> Grant | None:
        row = self._db.query_one("SELECT * FROM grants WHERE file_id = ?", (file_id,))
        if row is None:
            return None
        return Grant(
            file_id=row["file_id"],
            name=row["name"],
            mime_type=row["mime_type"],
            source=GrantSource(row["source"]),
            granted_at=from_iso(row["granted_at"]),
        )

    def require(self, file_id: str) -> Grant:
        """Raise unless this server holds a recorded grant for ``file_id``."""
        grant = self.get(file_id)
        if grant is None:
            raise NotGranted(
                f"No drive.file grant recorded for {file_id}. "
                "The app can only touch files it created or that you explicitly "
                "opened/shared with it; run `cwops grant <file_id>` to register one.",
                file_id=file_id,
            )
        return grant

    def is_granted(self, file_id: str) -> bool:
        return self.get(file_id) is not None

    def granted_ids(self) -> set[str]:
        return {row["file_id"] for row in self._db.query("SELECT file_id FROM grants")}

    def list_all(self) -> list[Grant]:
        rows = self._db.query("SELECT file_id FROM grants ORDER BY granted_at ASC")
        grants = [self.get(row["file_id"]) for row in rows]
        return [g for g in grants if g is not None]

    def revoke(self, file_id: str) -> bool:
        cursor = self._db.execute("DELETE FROM grants WHERE file_id = ?", (file_id,))
        return cursor.rowcount > 0
