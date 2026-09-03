"""The single write path.

Every mutation this system is capable of performing happens in ``_execute``
below. Reaching it requires passing three independent gates:

  1. ``dry_run=false`` must be requested explicitly - the default is a preview.
  2. ``CWOPS_ALLOW_MUTATIONS`` must be true - an operator-level switch.
  3. An unconsumed, unexpired approval bound to this exact plan hash must exist,
     and it is created only by `cwops approve`, a CLI command in a different
     process. No tool creates approvals, so a model has no path to one.

The plan is also re-validated against freshly fetched Drive state immediately
before execution: approval covers a specific plan against a specific world, and
if the world moved, the approval no longer applies.
"""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from ..container import Container
from ..errors import DriveApiError, MutationsDisabled, PlanRejected, ProposalNotPending
from ..models import (
    ApplyResult,
    CreateFolder,
    GrantSource,
    OpResult,
    Proposal,
    ProposalStatus,
    Rename,
    compute_plan_hash,
    summarize_op,
)
from ..rules import ValidationContext, validate_plan
from .base import audited
from .organize import _resolve_targets


def _revalidate(container: Container, proposal: Proposal) -> None:
    """Re-run full validation against current Drive state. Fail closed."""
    known = _resolve_targets(container, list(proposal.ops))
    ctx = ValidationContext(
        root_folder_id=container.workspace.root_folder_id,
        known_files=known,
        granted_ids=container.grants.granted_ids(),
        max_ops=max(container.settings.max_files_per_proposal, len(proposal.ops)),
    )
    accepted, report = validate_plan(list(proposal.ops), ctx)
    if len(accepted) != len(proposal.ops):
        raise PlanRejected(
            "The workspace changed since this plan was approved; "
            f"{len(proposal.ops) - len(accepted)} operation(s) no longer validate. "
            "Create a fresh proposal.",
            proposal_id=proposal.id,
            reasons=[rejection.reason_code for rejection in report.rejected],
        )
    if compute_plan_hash(proposal.root_folder_id, accepted) != proposal.plan_hash:
        raise PlanRejected(
            "Plan hash mismatch on re-validation.", proposal_id=proposal.id
        )


def _execute(container: Container, proposal: Proposal) -> tuple[list[OpResult], int, int]:
    """Run the operations in order, stopping at the first failure."""
    results: list[OpResult] = []
    ref_to_id: dict[str, str] = {}
    executed = 0
    failed = 0
    halted = False

    for index, op in enumerate(proposal.ops):
        summary = summarize_op(op)
        if halted:
            results.append(OpResult(index=index, summary=summary, status="skipped"))
            continue
        try:
            if isinstance(op, CreateFolder):
                parent = ref_to_id.get(op.parent_id, op.parent_id)
                folder = container.drive.create_folder(op.name, parent)
                ref_to_id[op.ref] = folder.id
                container.grants.record(folder, GrantSource.APP_CREATED)
                results.append(
                    OpResult(index=index, summary=summary, status="executed", file_id=folder.id)
                )
            elif isinstance(op, Rename):
                container.drive.rename(op.file_id, op.new_name)
                results.append(
                    OpResult(index=index, summary=summary, status="executed", file_id=op.file_id)
                )
            else:
                destination = ref_to_id.get(op.new_parent_id, op.new_parent_id)
                container.drive.move(op.file_id, destination, op.expected_old_parent_id)
                results.append(
                    OpResult(index=index, summary=summary, status="executed", file_id=op.file_id)
                )
            executed += 1
        except DriveApiError as exc:
            failed += 1
            halted = True
            results.append(
                OpResult(index=index, summary=summary, status="failed", detail=exc.detail)
            )
    return results, executed, failed


def register(server: MCPServer, container: Container) -> None:
    @server.tool(
        name="apply_proposal",
        description=(
            "Execute a stored proposal. This is the ONLY tool in this server "
            "that can change Google Drive. Defaults to dry_run=true, which "
            "previews the operations and touches nothing. A real run also "
            "requires CWOPS_ALLOW_MUTATIONS=true and a human approval created "
            "out-of-band with `cwops approve <proposal_id>`; approvals are "
            "single-use, time-limited, and bound to the exact plan. No delete, "
            "trash, overwrite or sharing operation exists - only create-folder, "
            "rename and move, all reversible."
        ),
    )
    def apply_proposal(proposal_id: str, dry_run: bool = True) -> ApplyResult:
        with audited(container, "apply_proposal", "apply") as ctx:
            ctx.proposal_id = proposal_id
            proposal = container.proposals.get(proposal_id)
            approval = container.proposals.approval_status(proposal)

            if proposal.status is not ProposalStatus.PENDING:
                raise ProposalNotPending(
                    f"Proposal {proposal_id} is already {proposal.status.value}.",
                    proposal_id=proposal_id,
                    status=proposal.status.value,
                )

            # Gate 1: dry run. Returns before any mutation gate is even consulted,
            # so a preview works safely even on a locked-down deployment.
            if dry_run:
                ctx.note(dry_run=True, ops=len(proposal.ops), approved=approval.approved)
                return ApplyResult(
                    proposal_id=proposal_id,
                    dry_run=True,
                    applied=False,
                    results=[
                        OpResult(index=index, summary=summarize_op(op), status="dry_run")
                        for index, op in enumerate(proposal.ops)
                    ],
                    executed=0,
                    failed=0,
                    correlation_id=ctx.correlation_id,
                    approval=approval,
                )

            # Gate 2: operator switch.
            if not container.settings.allow_mutations:
                raise MutationsDisabled(
                    "CWOPS_ALLOW_MUTATIONS is false; this server is read-only.",
                    proposal_id=proposal_id,
                )

            # The world must still match the plan that was approved.
            _revalidate(container, proposal)

            # Gate 3: human approval. Atomic, single-use, plan-bound, expiring.
            consumed = container.proposals.consume_approval(proposal)
            ctx.approved_by = consumed.approver

            results, executed, failed = _execute(container, proposal)
            status = ProposalStatus.FAILED if failed else ProposalStatus.APPLIED
            container.proposals.set_status(proposal_id, status)

            ctx.note(
                dry_run=False,
                executed=executed,
                failed=failed,
                approval_id=consumed.id,
            )
            return ApplyResult(
                proposal_id=proposal_id,
                dry_run=False,
                applied=failed == 0,
                results=results,
                executed=executed,
                failed=failed,
                correlation_id=ctx.correlation_id,
                approval=container.proposals.approval_status(proposal),
            )
