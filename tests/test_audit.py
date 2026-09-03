from __future__ import annotations

import json
from pathlib import Path

from cwops.logging import REDACTED
from cwops.store import AuditLog


def test_record_returns_row_id_and_roundtrips(audit: AuditLog) -> None:
    row_id = audit.record(
        actor="mcp_client",
        tool="drive_search",
        action="search",
        outcome="ok",
        detail={"query": "invoice", "results": 3},
    )
    assert row_id > 0
    rows = audit.recent()
    assert len(rows) == 1
    assert rows[0].tool == "drive_search"
    assert rows[0].outcome == "ok"
    assert rows[0].detail["results"] == 3


def test_refusals_are_recorded_not_swallowed(audit: AuditLog) -> None:
    audit.record(
        actor="mcp_client",
        tool="apply_proposal",
        action="apply",
        outcome="refused",
        proposal_id="prop_1",
        reason_code="approval_required",
    )
    rows = audit.for_proposal("prop_1")
    assert len(rows) == 1
    assert rows[0].outcome == "refused"
    assert rows[0].reason_code == "approval_required"


def test_approver_identity_is_captured(audit: AuditLog) -> None:
    audit.record(
        actor="cli",
        tool="cwops_approve",
        action="approve",
        outcome="ok",
        proposal_id="prop_2",
        approved_by="josephwquinn",
    )
    assert audit.for_proposal("prop_2")[0].approved_by == "josephwquinn"


def test_ordering_is_stable_across_a_proposal_lifecycle(audit: AuditLog) -> None:
    for action in ("propose", "approve", "apply"):
        audit.record(
            actor="cli", tool="t", action=action, outcome="ok", proposal_id="prop_3"
        )
    assert [row.action for row in audit.for_proposal("prop_3")] == [
        "propose",
        "approve",
        "apply",
    ]


def test_no_secret_ever_reaches_the_audit_table(audit: AuditLog, db) -> None:  # type: ignore[no-untyped-def]
    """I6: even if a caller passes a credential in, it must not be persisted."""
    audit.record(
        actor="system",
        tool="drive_auth",
        action="refresh",
        outcome="ok",
        detail={
            "access_token": "ya29.super-secret-value",
            "nested": {"ANTHROPIC_API_KEY": "sk-ant-leaked"},
            "note": "sk-ant-in-an-innocent-field",
            "file_name": "Q3 invoice.pdf",
        },
    )
    raw = "".join(str(tuple(row)) for row in db.query("SELECT * FROM audit"))
    assert "ya29.super-secret-value" not in raw
    assert "sk-ant-leaked" not in raw
    assert "sk-ant-in-an-innocent-field" not in raw
    assert raw.count(REDACTED) >= 3
    # Non-secret context survives, otherwise the log would be useless.
    assert "Q3 invoice.pdf" in raw


def test_export_jsonl_writes_one_json_object_per_row(audit: AuditLog, tmp_path: Path) -> None:
    for index in range(3):
        audit.record(actor="cli", tool="t", action=f"a{index}", outcome="ok")
    target = tmp_path / "export" / "audit.jsonl"
    written = audit.export_jsonl(target)
    lines = target.read_text(encoding="utf-8").strip().splitlines()
    assert written == 3
    assert len(lines) == 3
    assert json.loads(lines[0])["action"] == "a0"


def test_count_tracks_appends(audit: AuditLog) -> None:
    assert audit.count() == 0
    audit.record(actor="cli", tool="t", action="a", outcome="ok")
    assert audit.count() == 1
