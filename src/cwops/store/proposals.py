"""Proposals and the human approval gate.

The security property this file exists to provide:

    An approval row can only be created by `ProposalStore.approve`, which is
    reachable from the CLI (`cwops approve`) and nothing else. No MCP tool
    calls it. A model driving this server therefore has no code path that
    produces an approval, and cannot forge one.

Approvals are bound to a plan hash, single-use, and TTL-limited.
"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta

from ..clock import Clock, SystemClock
from ..errors import (
    ApprovalInvalid,
    ApprovalRequired,
    PlanRejected,
    ProposalNotFound,
    ProposalNotPending,
)
from ..models import Approval, ApprovalStatus, Proposal, ProposalStatus
from .db import Database, from_iso, to_iso


def new_proposal_id() -> str:
    return f"prop_{uuid.uuid4().hex[:12]}"


class ProposalStore:
    def __init__(
        self,
        db: Database,
        clock: Clock | None = None,
        approval_ttl_seconds: int = 900,
    ) -> None:
        self._db = db
        self._clock = clock or SystemClock()
        self._ttl = approval_ttl_seconds

    # -- proposals ---------------------------------------------------------

    def create(self, proposal: Proposal) -> Proposal:
        self._db.execute(
            """
            INSERT INTO proposals
                (id, created_at, kind, root_folder_id, plan_hash, status, payload)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                proposal.id,
                to_iso(proposal.created_at),
                proposal.kind,
                proposal.root_folder_id,
                proposal.plan_hash,
                proposal.status.value,
                proposal.model_dump_json(),
            ),
        )
        return proposal

    def get(self, proposal_id: str) -> Proposal:
        row = self._db.query_one("SELECT payload FROM proposals WHERE id = ?", (proposal_id,))
        if row is None:
            raise ProposalNotFound(f"No proposal {proposal_id}", proposal_id=proposal_id)
        return Proposal.model_validate(json.loads(row["payload"]))

    def list(self, status: ProposalStatus | None = None, limit: int = 50) -> list[Proposal]:
        if status is None:
            rows = self._db.query(
                "SELECT payload FROM proposals ORDER BY created_at DESC LIMIT ?", (limit,)
            )
        else:
            rows = self._db.query(
                "SELECT payload FROM proposals WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                (status.value, limit),
            )
        return [Proposal.model_validate(json.loads(row["payload"])) for row in rows]

    def set_status(self, proposal_id: str, status: ProposalStatus) -> Proposal:
        proposal = self.get(proposal_id)
        updated = proposal.model_copy(update={"status": status})
        self._db.execute(
            "UPDATE proposals SET status = ?, payload = ? WHERE id = ?",
            (status.value, updated.model_dump_json(), proposal_id),
        )
        return updated

    # -- approvals ---------------------------------------------------------

    def approve(self, proposal_id: str, approver: str) -> Approval:
        """Create a human approval. CLI-only entry point - never exposed as a tool."""
        proposal = self.get(proposal_id)
        if proposal.status is not ProposalStatus.PENDING:
            raise ProposalNotPending(
                f"Proposal {proposal_id} is {proposal.status.value}, not pending",
                proposal_id=proposal_id,
                status=proposal.status.value,
            )
        if not proposal.validation.ok:
            raise PlanRejected(
                f"Proposal {proposal_id} failed deterministic validation and cannot be approved",
                proposal_id=proposal_id,
                rejected=len(proposal.validation.rejected),
            )
        now = self._clock.now()
        approval = Approval(
            id=f"appr_{uuid.uuid4().hex[:12]}",
            proposal_id=proposal_id,
            plan_hash=proposal.plan_hash,
            approver=approver,
            created_at=now,
            expires_at=now + timedelta(seconds=self._ttl),
        )
        self._db.execute(
            """
            INSERT INTO approvals
                (id, proposal_id, plan_hash, approver, created_at, expires_at, consumed_at)
            VALUES (?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                approval.id,
                approval.proposal_id,
                approval.plan_hash,
                approval.approver,
                to_iso(approval.created_at),
                to_iso(approval.expires_at),
            ),
        )
        return approval

    def latest_approval(self, proposal_id: str) -> Approval | None:
        row = self._db.query_one(
            "SELECT * FROM approvals WHERE proposal_id = ?"
            " ORDER BY created_at DESC, id DESC LIMIT 1",
            (proposal_id,),
        )
        if row is None:
            return None
        return Approval(
            id=row["id"],
            proposal_id=row["proposal_id"],
            plan_hash=row["plan_hash"],
            approver=row["approver"],
            created_at=from_iso(row["created_at"]),
            expires_at=from_iso(row["expires_at"]),
            consumed_at=from_iso(row["consumed_at"]) if row["consumed_at"] else None,
        )

    def approval_status(self, proposal: Proposal) -> ApprovalStatus:
        approval = self.latest_approval(proposal.id)
        if approval is None:
            return ApprovalStatus(approved=False)
        now = self._clock.now()
        hash_matches = approval.plan_hash == proposal.plan_hash
        consumed = approval.consumed_at is not None
        live = not consumed and approval.expires_at > now and hash_matches
        return ApprovalStatus(
            approved=live,
            approver=approval.approver,
            approved_at=approval.created_at,
            expires_at=approval.expires_at,
            consumed=consumed,
            plan_hash_matches=hash_matches,
        )

    def consume_approval(self, proposal: Proposal) -> Approval:
        """Atomically claim the approval for this exact plan.

        Raises rather than returning a falsy value: there is no code path in
        which an unapproved apply silently proceeds.
        """
        approval = self.latest_approval(proposal.id)
        if approval is None:
            raise ApprovalRequired(
                f"Proposal {proposal.id} has no human approval. "
                f"Run: cwops approve {proposal.id}",
                proposal_id=proposal.id,
            )
        now = self._clock.now()
        if approval.plan_hash != proposal.plan_hash:
            raise ApprovalInvalid(
                "Approval was issued for a different version of this plan; re-approve it.",
                proposal_id=proposal.id,
                approval_id=approval.id,
                cause="plan_hash_mismatch",
            )
        if approval.consumed_at is not None:
            raise ApprovalInvalid(
                "Approval has already been used; approvals are single-use.",
                proposal_id=proposal.id,
                approval_id=approval.id,
                cause="already_consumed",
            )
        if approval.expires_at <= now:
            raise ApprovalInvalid(
                f"Approval expired at {approval.expires_at.isoformat()}; re-approve it.",
                proposal_id=proposal.id,
                approval_id=approval.id,
                cause="expired",
            )

        # Single UPDATE guarded by every precondition: even under a concurrent
        # apply, exactly one caller can claim the approval.
        cursor = self._db.execute(
            """
            UPDATE approvals SET consumed_at = ?
            WHERE id = ? AND consumed_at IS NULL AND plan_hash = ? AND expires_at > ?
            """,
            (to_iso(now), approval.id, proposal.plan_hash, to_iso(now)),
        )
        if cursor.rowcount != 1:
            raise ApprovalInvalid(
                "Approval could not be claimed; it was consumed or invalidated concurrently.",
                proposal_id=proposal.id,
                approval_id=approval.id,
                cause="claim_lost",
            )
        return approval.model_copy(update={"consumed_at": now})
