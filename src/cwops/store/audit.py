"""Append-only audit log.

Every tool invocation writes exactly one row: what was requested, which tool
served it, the outcome, the reason code on refusal, and who approved it. Rows
are never updated or deleted by application code.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from ..clock import Clock, SystemClock
from ..logging import get_correlation_id, scrub
from ..models import AuditRow
from .db import Database, from_iso, to_iso

Actor = Literal["mcp_client", "cli", "system"]
Outcome = Literal["ok", "refused", "error"]


class AuditLog:
    def __init__(self, db: Database, clock: Clock | None = None) -> None:
        self._db = db
        self._clock = clock or SystemClock()

    def record(
        self,
        *,
        actor: Actor,
        tool: str,
        action: str,
        outcome: Outcome,
        proposal_id: str | None = None,
        reason_code: str | None = None,
        approved_by: str | None = None,
        correlation_id: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> int:
        """Append one row. ``detail`` is scrubbed before it touches disk."""
        safe_detail = scrub(detail or {})
        cursor = self._db.execute(
            """
            INSERT INTO audit
                (ts, correlation_id, actor, tool, action, outcome,
                 proposal_id, reason_code, approved_by, detail)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                to_iso(self._clock.now()),
                correlation_id or get_correlation_id(),
                actor,
                tool,
                action,
                outcome,
                proposal_id,
                reason_code,
                approved_by,
                json.dumps(safe_detail, default=str, sort_keys=True),
            ),
        )
        return int(cursor.lastrowid or 0)

    def recent(self, limit: int = 50) -> list[AuditRow]:
        rows = self._db.query("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))
        return [self._row_to_model(row) for row in rows]

    def for_proposal(self, proposal_id: str) -> list[AuditRow]:
        rows = self._db.query(
            "SELECT * FROM audit WHERE proposal_id = ? ORDER BY id ASC", (proposal_id,)
        )
        return [self._row_to_model(row) for row in rows]

    def count(self) -> int:
        row = self._db.query_one("SELECT COUNT(*) AS n FROM audit")
        return int(row["n"]) if row else 0

    def export_jsonl(self, path: Path) -> int:
        """Write the whole log as JSON Lines. Used by `cwops audit export`."""
        rows = self._db.query("SELECT * FROM audit ORDER BY id ASC")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(self._row_to_model(row).model_dump_json() + "\n")
        return len(rows)

    @staticmethod
    def _row_to_model(row: Any) -> AuditRow:
        return AuditRow(
            id=row["id"],
            ts=from_iso(row["ts"]),
            correlation_id=row["correlation_id"],
            actor=row["actor"],
            tool=row["tool"],
            action=row["action"],
            outcome=row["outcome"],
            proposal_id=row["proposal_id"],
            reason_code=row["reason_code"],
            approved_by=row["approved_by"],
            detail=json.loads(row["detail"]),
        )
