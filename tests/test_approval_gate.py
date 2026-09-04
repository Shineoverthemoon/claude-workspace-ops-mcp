"""The approval gate.

Three independent gates stand between a proposal and a real mutation:

  1. dry_run must be explicitly false
  2. CWOPS_ALLOW_MUTATIONS must be true
  3. an unconsumed, unexpired, plan-bound human approval must exist

Approvals are created only by ProposalStore.approve, reachable from the CLI and
from no MCP tool. The first test below is the load-bearing one: it proves there
is no tool a model could call to approve its own plan.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from conftest import ROOT_ID, ToolCaller, make_caller
from cwops.container import Container
from cwops.drive import FakeDriveClient
from cwops.errors import ApprovalInvalid, ApprovalRequired, PlanRejected, ProposalNotPending
from cwops.models import ApprovalState, CreateFolder, ProposalStatus, compute_plan_hash
from cwops.server import build_server

APPROVER = "test-operator"


def make_proposal(call: ToolCaller) -> str:
    """A small, always-valid manual plan."""
    proposal = call(
        "propose_operations",
        ops=[
            {
                "op": "create_folder",
                "ref": "#invoices",
                "name": "Invoices",
                "parent_id": ROOT_ID,
            },
            {"op": "move", "file_id": "f-inv-q1", "new_parent_id": "#invoices"},
        ],
    )
    assert proposal["validation"]["ok"] is True
    assert proposal["validation"]["rejected"] == []
    return str(proposal["id"])


# --- the structural guarantee ---------------------------------------------


def test_no_tool_can_create_an_approval(container: Container) -> None:
    """A model driving this server has no reachable path to an approval."""
    server = build_server(container)
    names = {tool.name for tool in asyncio.run(server.list_tools())}
    assert not any(
        word in name for name in names for word in ("approve", "approval", "confirm", "grant")
    )


def test_approval_records_come_only_from_the_cli_path(
    call: ToolCaller, container: Container
) -> None:
    proposal_id = make_proposal(call)
    # Exercising every tool must not produce an approval.
    call("get_proposal", proposal_id=proposal_id)
    call("apply_proposal", proposal_id=proposal_id)
    assert container.proposals.latest_approval(proposal_id) is None


# --- gate 3: approval required --------------------------------------------


def test_apply_without_approval_is_refused(call: ToolCaller, drive: FakeDriveClient) -> None:
    proposal_id = make_proposal(call)
    with pytest.raises(ToolError) as exc:
        call("apply_proposal", proposal_id=proposal_id, dry_run=False)
    assert "approval_required" in str(exc.value)
    assert drive.mutating_calls == []


def test_apply_with_approval_succeeds(
    call: ToolCaller, container: Container, drive: FakeDriveClient
) -> None:
    proposal_id = make_proposal(call)
    container.proposals.approve(proposal_id, APPROVER)

    result = call("apply_proposal", proposal_id=proposal_id, dry_run=False)

    assert result["applied"] is True
    assert result["executed"] == 2
    assert result["failed"] == 0
    assert [c.method for c in drive.mutating_calls] == ["create_folder", "move"]
    moved = drive.get_file("f-inv-q1")
    assert moved.parents != ["fol-inbox"]


# --- approvals are single-use ---------------------------------------------


def test_an_approval_cannot_be_used_twice(call: ToolCaller, container: Container) -> None:
    proposal_id = make_proposal(call)
    container.proposals.approve(proposal_id, APPROVER)
    call("apply_proposal", proposal_id=proposal_id, dry_run=False)

    with pytest.raises(ToolError) as exc:
        call("apply_proposal", proposal_id=proposal_id, dry_run=False)
    # The proposal is now applied, so it is refused before the approval is
    # even consulted - defence in depth.
    assert "proposal_not_pending" in str(exc.value)


def test_consuming_an_approval_twice_is_refused_at_the_store(
    call: ToolCaller, container: Container
) -> None:
    proposal_id = make_proposal(call)
    container.proposals.approve(proposal_id, APPROVER)
    proposal = container.proposals.get(proposal_id)

    container.proposals.consume_approval(proposal)
    with pytest.raises(ApprovalInvalid) as exc:
        container.proposals.consume_approval(proposal)
    assert exc.value.context["cause"] == "already_consumed"


# --- approval state stays legible after the fact ---------------------------


def test_a_spent_approval_is_not_reported_as_never_approved(
    call: ToolCaller, container: Container
) -> None:
    """History has to survive the apply: `consumed` is not the same as `none`."""
    proposal_id = make_proposal(call)
    assert call("get_proposal", proposal_id=proposal_id)["approval"]["state"] == "none"

    container.proposals.approve(proposal_id, APPROVER)
    assert call("get_proposal", proposal_id=proposal_id)["approval"]["state"] == "active"

    result = call("apply_proposal", proposal_id=proposal_id, dry_run=False)
    approval = result["approval"]
    assert approval["state"] == "consumed"
    assert approval["consumed"] is True
    assert approval["approver"] == APPROVER
    # Still not usable again - the state field explains history, it does not
    # authorize anything.
    assert approval["approved"] is False


def test_an_expired_approval_reports_its_own_state(
    call: ToolCaller, container: Container, clock: object
) -> None:
    proposal_id = make_proposal(call)
    container.proposals.approve(proposal_id, APPROVER)
    clock.advance(container.settings.approval_ttl_seconds + 1)  # type: ignore[attr-defined]

    status = container.proposals.approval_status(container.proposals.get(proposal_id))
    assert status.approved is False
    assert status.state is ApprovalState.EXPIRED
    assert status.consumed is False


# --- approvals are bound to one plan --------------------------------------


def test_an_approval_for_a_different_proposal_is_not_accepted(
    call: ToolCaller, container: Container, drive: FakeDriveClient
) -> None:
    first = make_proposal(call)
    second = call(
        "propose_operations",
        ops=[{"op": "rename", "file_id": "f-inv-q2", "new_name": "Q2.pdf"}],
    )["id"]
    container.proposals.approve(first, APPROVER)

    with pytest.raises(ToolError) as exc:
        call("apply_proposal", proposal_id=second, dry_run=False)
    assert "approval_required" in str(exc.value)
    assert drive.mutating_calls == []


def test_editing_a_plan_after_approval_invalidates_it(
    call: ToolCaller, container: Container, drive: FakeDriveClient
) -> None:
    """The plan hash is the binding: tamper with the ops and the approval dies."""
    proposal_id = make_proposal(call)
    container.proposals.approve(proposal_id, APPROVER)

    tampered = container.proposals.get(proposal_id)
    tampered.ops.append(
        CreateFolder(ref="#sneaky", name="Sneaky", parent_id=ROOT_ID)  # type: ignore[arg-type]
    )
    tampered.plan_hash = compute_plan_hash(tampered.root_folder_id, list(tampered.ops))
    container.db.execute(
        "UPDATE proposals SET plan_hash = ?, payload = ? WHERE id = ?",
        (tampered.plan_hash, tampered.model_dump_json(), proposal_id),
    )

    with pytest.raises(ApprovalInvalid) as exc:
        container.proposals.consume_approval(container.proposals.get(proposal_id))
    assert exc.value.context["cause"] == "plan_hash_mismatch"
    assert drive.mutating_calls == []


def test_approval_status_reports_a_hash_mismatch(
    call: ToolCaller, container: Container
) -> None:
    proposal_id = make_proposal(call)
    container.proposals.approve(proposal_id, APPROVER)
    proposal = container.proposals.get(proposal_id)
    altered = proposal.model_copy(update={"plan_hash": "0" * 64})
    status = container.proposals.approval_status(altered)
    assert status.approved is False
    assert status.plan_hash_matches is False
    assert status.state is ApprovalState.SUPERSEDED


# --- approvals expire ------------------------------------------------------


def test_an_expired_approval_is_refused(
    call: ToolCaller, container: Container, clock: object, drive: FakeDriveClient
) -> None:
    proposal_id = make_proposal(call)
    container.proposals.approve(proposal_id, APPROVER)
    clock.advance(container.settings.approval_ttl_seconds + 1)  # type: ignore[attr-defined]

    with pytest.raises(ToolError) as exc:
        call("apply_proposal", proposal_id=proposal_id, dry_run=False)
    assert "approval_invalid" in str(exc.value)
    assert drive.mutating_calls == []


def test_an_approval_one_second_before_expiry_still_works(
    call: ToolCaller, container: Container, clock: object
) -> None:
    proposal_id = make_proposal(call)
    container.proposals.approve(proposal_id, APPROVER)
    clock.advance(container.settings.approval_ttl_seconds - 1)  # type: ignore[attr-defined]
    assert call("apply_proposal", proposal_id=proposal_id, dry_run=False)["applied"] is True


# --- gate 2: the operator switch -------------------------------------------


def test_mutations_disabled_blocks_even_an_approved_plan(
    make_container: Callable[..., Container], drive: FakeDriveClient
) -> None:
    container = make_container(allow_mutations=False)
    try:
        call = make_caller(container)
        proposal_id = make_proposal(call)
        container.proposals.approve(proposal_id, APPROVER)

        with pytest.raises(ToolError) as exc:
            call("apply_proposal", proposal_id=proposal_id, dry_run=False)
        assert "mutations_disabled" in str(exc.value)
        assert drive.mutating_calls == []
        # And the approval was NOT burned by the refused attempt.
        assert container.proposals.latest_approval(proposal_id).consumed_at is None  # type: ignore[union-attr]
    finally:
        container.close()


# --- the world must still match the plan -----------------------------------


def test_approval_does_not_survive_the_world_changing(
    call: ToolCaller, container: Container, drive: FakeDriveClient
) -> None:
    proposal_id = make_proposal(call)
    container.proposals.approve(proposal_id, APPROVER)

    # The user revokes this app's access to the file between approval and apply.
    drive.revoke("f-inv-q1")
    container.grants.revoke("f-inv-q1")

    with pytest.raises(ToolError) as exc:
        call("apply_proposal", proposal_id=proposal_id, dry_run=False)
    assert "plan_rejected" in str(exc.value)
    assert drive.mutating_calls == []


# --- refusals are never silent ---------------------------------------------


@pytest.mark.parametrize(
    ("scenario", "reason_code"),
    [
        ("no_approval", "approval_required"),
        ("already_applied", "proposal_not_pending"),
    ],
)
def test_every_refusal_is_audited(
    call: ToolCaller, container: Container, scenario: str, reason_code: str
) -> None:
    proposal_id = make_proposal(call)
    if scenario == "already_applied":
        container.proposals.approve(proposal_id, APPROVER)
        call("apply_proposal", proposal_id=proposal_id, dry_run=False)

    with pytest.raises(ToolError):
        call("apply_proposal", proposal_id=proposal_id, dry_run=False)

    rows = [row for row in container.audit.for_proposal(proposal_id) if row.outcome == "refused"]
    assert rows, "a refusal must leave an audit row"
    assert rows[-1].reason_code == reason_code


def test_a_successful_apply_records_the_approver(
    call: ToolCaller, container: Container
) -> None:
    proposal_id = make_proposal(call)
    container.proposals.approve(proposal_id, APPROVER)
    call("apply_proposal", proposal_id=proposal_id, dry_run=False)

    applied = [
        row
        for row in container.audit.for_proposal(proposal_id)
        if row.tool == "apply_proposal" and row.outcome == "ok" and not row.detail.get("dry_run")
    ]
    assert applied[-1].approved_by == APPROVER


def test_proposal_status_advances_to_applied(call: ToolCaller, container: Container) -> None:
    proposal_id = make_proposal(call)
    container.proposals.approve(proposal_id, APPROVER)
    call("apply_proposal", proposal_id=proposal_id, dry_run=False)
    assert container.proposals.get(proposal_id).status is ProposalStatus.APPLIED


# --- store-level errors surface with the right types -----------------------


def test_store_raises_approval_required_when_none_exists(
    call: ToolCaller, container: Container
) -> None:
    proposal = container.proposals.get(make_proposal(call))
    with pytest.raises(ApprovalRequired) as exc:
        container.proposals.consume_approval(proposal)
    assert "cwops approve" in exc.value.detail


def test_an_invalid_plan_cannot_be_approved(call: ToolCaller, container: Container) -> None:
    proposal = call(
        "propose_operations",
        ops=[{"op": "rename", "file_id": "f-hallucinated", "new_name": "x.pdf"}],
    )
    assert proposal["validation"]["ok"] is False
    with pytest.raises(PlanRejected):
        container.proposals.approve(proposal["id"], APPROVER)


def test_an_applied_proposal_cannot_be_re_approved(
    call: ToolCaller, container: Container
) -> None:
    proposal_id = make_proposal(call)
    container.proposals.approve(proposal_id, APPROVER)
    call("apply_proposal", proposal_id=proposal_id, dry_run=False)
    with pytest.raises(ProposalNotPending):
        container.proposals.approve(proposal_id, APPROVER)


# --- execution ordering ----------------------------------------------------


def test_refs_are_bound_to_real_ids_during_execution(
    call: ToolCaller, container: Container, drive: FakeDriveClient
) -> None:
    proposal_id = make_proposal(call)
    container.proposals.approve(proposal_id, APPROVER)
    result = call("apply_proposal", proposal_id=proposal_id, dry_run=False)

    created_id = result["results"][0]["file_id"]
    assert created_id and not created_id.startswith("#")
    assert drive.get_file("f-inv-q1").parents == [created_id]
    assert container.grants.get(created_id) is not None


def test_a_failure_halts_the_run_and_skips_the_rest(
    call: ToolCaller, container: Container, drive: FakeDriveClient
) -> None:
    proposal = call(
        "propose_operations",
        ops=[
            {"op": "rename", "file_id": "f-inv-q1", "new_name": "One.pdf"},
            {"op": "rename", "file_id": "f-inv-q2", "new_name": "Two.pdf"},
            {"op": "rename", "file_id": "f-contract", "new_name": "Three.pdf"},
        ],
    )
    container.proposals.approve(proposal["id"], APPROVER)
    from cwops.errors import DriveApiError

    drive.queue_error("rename", DriveApiError("upstream exploded", status=500), "f-inv-q2")

    result = call("apply_proposal", proposal_id=proposal["id"], dry_run=False)

    assert [row["status"] for row in result["results"]] == ["executed", "failed", "skipped"]
    assert result["applied"] is False
    assert container.proposals.get(proposal["id"]).status is ProposalStatus.FAILED
    assert drive.get_file("f-contract").name == "Vendor Contract - Acme.pdf"


def test_move_detects_the_file_moving_under_us(
    call: ToolCaller, container: Container, drive: FakeDriveClient
) -> None:
    """expected_old_parent_id gives optimistic concurrency at apply time."""
    proposal = call(
        "propose_operations",
        ops=[
            {
                "op": "move",
                "file_id": "f-inv-q1",
                "new_parent_id": "fol-archive",
                "expected_old_parent_id": "fol-inbox",
            }
        ],
    )
    container.proposals.approve(proposal["id"], APPROVER)
    drive.move("f-inv-q1", "fol-archive-2025")  # someone else moves it first

    result = call("apply_proposal", proposal_id=proposal["id"], dry_run=False)
    assert result["results"][0]["status"] == "failed"
    assert result["applied"] is False
