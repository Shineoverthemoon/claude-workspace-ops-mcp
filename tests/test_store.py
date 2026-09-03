"""Persistence round-trips for the grant ledger and proposal store.

The approval *gate* semantics are exercised separately in test_approval_gate.py;
this file covers durable storage behaviour only.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from cwops.errors import NotGranted, ProposalNotFound
from cwops.models import (
    CreateFolder,
    DriveFile,
    GrantSource,
    Move,
    Proposal,
    ProposalStatus,
    ValidationReport,
    compute_plan_hash,
)
from cwops.store import GrantLedger, ProposalStore


def make_file(file_id: str = "f1", name: str = "Report.pdf") -> DriveFile:
    return DriveFile(id=file_id, name=name, mime_type="application/pdf", parents=["root-workspace"])


def make_proposal(proposal_id: str = "prop_test") -> Proposal:
    ops = [
        CreateFolder(ref="#invoices", name="Invoices", parent_id="root-workspace"),
        Move(file_id="f1", new_parent_id="#invoices"),
    ]
    return Proposal(
        id=proposal_id,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        kind="manual",
        root_folder_id="root-workspace",
        ops=ops,
        plan_hash=compute_plan_hash("root-workspace", ops),
        preview=["create folder", "move file"],
        validation=ValidationReport(ok=True, accepted=2),
    )


# --- grants ---------------------------------------------------------------


def test_grant_record_and_require(grants: GrantLedger) -> None:
    grants.record(make_file(), GrantSource.APP_CREATED)
    grant = grants.require("f1")
    assert grant.source is GrantSource.APP_CREATED
    assert grant.name == "Report.pdf"


def test_require_raises_for_ungranted_file(grants: GrantLedger) -> None:
    with pytest.raises(NotGranted) as exc:
        grants.require("never-seen")
    assert exc.value.reason_code == "not_granted"
    assert "drive.file" in exc.value.detail


def test_record_is_idempotent_and_updates_metadata(grants: GrantLedger) -> None:
    grants.record(make_file(name="Old.pdf"), GrantSource.APP_CREATED)
    grants.record(make_file(name="New.pdf"), GrantSource.VERIFIED)
    assert len(grants.list_all()) == 1
    grant = grants.require("f1")
    assert grant.name == "New.pdf"
    assert grant.source is GrantSource.VERIFIED


def test_granted_ids_and_revoke(grants: GrantLedger) -> None:
    grants.record_many([make_file("a"), make_file("b")], GrantSource.APP_CREATED)
    assert grants.granted_ids() == {"a", "b"}
    assert grants.revoke("a") is True
    assert grants.revoke("a") is False
    assert grants.granted_ids() == {"b"}


def test_picker_source_is_representable_for_deferred_integration() -> None:
    assert GrantSource.PICKER.value == "picker"


# --- proposals ------------------------------------------------------------


def test_proposal_roundtrip_preserves_discriminated_ops(proposals: ProposalStore) -> None:
    proposals.create(make_proposal())
    loaded = proposals.get("prop_test")
    assert isinstance(loaded.ops[0], CreateFolder)
    assert isinstance(loaded.ops[1], Move)
    assert loaded.ops[1].new_parent_id == "#invoices"
    assert loaded.status is ProposalStatus.PENDING


def test_missing_proposal_raises(proposals: ProposalStore) -> None:
    with pytest.raises(ProposalNotFound) as exc:
        proposals.get("nope")
    assert exc.value.reason_code == "proposal_not_found"


def test_set_status_persists(proposals: ProposalStore) -> None:
    proposals.create(make_proposal())
    proposals.set_status("prop_test", ProposalStatus.APPLIED)
    assert proposals.get("prop_test").status is ProposalStatus.APPLIED
    assert proposals.list(status=ProposalStatus.PENDING) == []
    assert len(proposals.list(status=ProposalStatus.APPLIED)) == 1


def test_plan_hash_is_order_sensitive_and_content_sensitive() -> None:
    a = CreateFolder(ref="#x", name="X", parent_id="root")
    b = CreateFolder(ref="#y", name="Y", parent_id="root")
    assert compute_plan_hash("root", [a, b]) != compute_plan_hash("root", [b, a])
    assert compute_plan_hash("root", [a]) != compute_plan_hash("other-root", [a])
    assert compute_plan_hash("root", [a]) == compute_plan_hash("root", [a])
