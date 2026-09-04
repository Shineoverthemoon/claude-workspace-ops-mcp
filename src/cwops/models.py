"""Shared domain types.

These Pydantic models are the MCP tool contract: FastMCP derives the published
JSON Schema from them, so a change here is a public API change and is caught by
the golden-schema snapshot test.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

FOLDER_MIME = "application/vnd.google-apps.folder"
GOOGLE_NATIVE_PREFIX = "application/vnd.google-apps."
REF_PREFIX = "#"


def is_ref(value: str) -> bool:
    """True if ``value`` names a folder created earlier in the same plan."""
    return value.startswith(REF_PREFIX)


# --------------------------------------------------------------------------
# Drive
# --------------------------------------------------------------------------


class DriveFile(BaseModel):
    """A Drive file as this server sees it. Metadata only - never file bytes."""

    model_config = ConfigDict(populate_by_name=True)

    id: str
    name: str
    mime_type: str = Field(description="Drive MIME type")
    parents: list[str] = Field(default_factory=list)
    size: int | None = Field(default=None, description="Bytes; absent for Google-native docs")
    md5_checksum: str | None = Field(
        default=None, description="Absent for Google-native docs and folders"
    )
    modified_time: datetime | None = None
    trashed: bool = False

    @property
    def is_folder(self) -> bool:
        return self.mime_type == FOLDER_MIME

    @property
    def is_google_native(self) -> bool:
        return self.mime_type.startswith(GOOGLE_NATIVE_PREFIX)


class GrantSource(StrEnum):
    """How the server obtained drive.file access to a file.

    ``PICKER`` is reserved for the deferred Google Picker integration; recording
    it now means adding Picker later needs no schema change.
    """

    APP_CREATED = "app_created"
    VERIFIED = "verified"
    PICKER = "picker"


class Grant(BaseModel):
    """A ledger entry: proof this server may touch one specific file."""

    file_id: str
    name: str
    mime_type: str
    source: GrantSource
    granted_at: datetime


# --------------------------------------------------------------------------
# Operations
# --------------------------------------------------------------------------


class CreateFolder(BaseModel):
    """Create a folder. ``ref`` lets later ops in the same plan target it."""

    op: Literal["create_folder"] = "create_folder"
    ref: str = Field(
        description="Plan-local handle, e.g. '#invoices'",
        pattern=r"^#[a-z0-9_\-]{1,40}$",
    )
    name: str
    parent_id: str = Field(description="Real folder ID, or a '#ref' declared earlier")


class Rename(BaseModel):
    op: Literal["rename"] = "rename"
    file_id: str
    new_name: str


class Move(BaseModel):
    op: Literal["move"] = "move"
    file_id: str
    new_parent_id: str = Field(description="Real folder ID, or a '#ref' declared earlier")
    expected_old_parent_id: str | None = Field(
        default=None, description="Optimistic-concurrency check performed at apply time"
    )


Operation = Annotated[CreateFolder | Rename | Move, Field(discriminator="op")]

# No delete, trash, overwrite, permission or sharing operation exists in the
# MVP. Every operation above is reversible.


def summarize_op(op: CreateFolder | Rename | Move) -> str:
    if isinstance(op, CreateFolder):
        return f"create_folder {op.name!r} in {op.parent_id} as {op.ref}"
    if isinstance(op, Rename):
        return f"rename {op.file_id} -> {op.new_name!r}"
    return f"move {op.file_id} -> {op.new_parent_id}"


def compute_plan_hash(root_folder_id: str, ops: list[CreateFolder | Rename | Move]) -> str:
    """Deterministic fingerprint binding an approval to an exact plan.

    Any edit to the operation list invalidates every existing approval.
    """
    canonical = {
        "root": root_folder_id,
        "ops": [op.model_dump(mode="json") for op in ops],
    }
    blob = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# Validation + proposals
# --------------------------------------------------------------------------


class RejectedOp(BaseModel):
    index: int
    summary: str
    reason_code: str
    detail: str


class ValidationReport(BaseModel):
    ok: bool
    accepted: int
    rejected: list[RejectedOp] = Field(default_factory=list)


class ProposalStatus(StrEnum):
    PENDING = "pending"
    APPLIED = "applied"
    FAILED = "failed"


class Proposal(BaseModel):
    """An immutable, validated plan awaiting out-of-band human approval."""

    id: str
    created_at: datetime
    kind: Literal["manual", "organization"]
    root_folder_id: str
    ops: list[Operation]
    plan_hash: str
    preview: list[str] = Field(description="Human-readable diff, one line per operation")
    validation: ValidationReport
    status: ProposalStatus = ProposalStatus.PENDING
    granted_file_ids: list[str] = Field(
        default_factory=list, description="Grant snapshot this plan was validated against"
    )
    notes: str | None = None


class ApprovalState(StrEnum):
    """What became of the human approval for a proposal.

    ``ApprovalStatus.approved`` answers "may this plan run right now?", which is
    false for a plan that ran successfully an hour ago. This field answers "what
    happened?", so a spent approval stays distinguishable from one that was
    never granted.
    """

    NONE = "none"
    ACTIVE = "active"
    CONSUMED = "consumed"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"


class ApprovalStatus(BaseModel):
    approved: bool = Field(description="Usable right now: unspent, unexpired, plan matches")
    state: ApprovalState = Field(
        default=ApprovalState.NONE, description="Lifecycle of the approval, including history"
    )
    approver: str | None = None
    approved_at: datetime | None = None
    expires_at: datetime | None = None
    consumed: bool = False
    plan_hash_matches: bool | None = None


class ProposalView(BaseModel):
    """What `get_proposal` returns: the plan plus its live approval state."""

    proposal: Proposal
    approval: ApprovalStatus


class Approval(BaseModel):
    id: str
    proposal_id: str
    plan_hash: str
    approver: str
    created_at: datetime
    expires_at: datetime
    consumed_at: datetime | None = None


# --------------------------------------------------------------------------
# Tool results
# --------------------------------------------------------------------------


class SearchResult(BaseModel):
    files: list[DriveFile]
    truncated: bool = False
    note: str | None = None


class FileContent(BaseModel):
    file_id: str
    name: str
    mime_type: str
    text: str
    truncated: bool
    chars: int


class DuplicateGroup(BaseModel):
    reason: Literal["checksum", "metadata"]
    confidence: Literal["exact", "heuristic"]
    key: str
    files: list[DriveFile]


class UnscannableFile(BaseModel):
    file_id: str
    name: str
    mime_type: str
    reason: str


class DuplicateReport(BaseModel):
    strategy: Literal["checksum", "metadata"]
    scanned: int
    groups: list[DuplicateGroup]
    unscannable: list[UnscannableFile]


class Classification(BaseModel):
    """Claude's judgment about one file. Advisory - never an authorization."""

    file_id: str
    category: str
    topics: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str


class DraftFolder(BaseModel):
    ref: str = Field(pattern=r"^#[a-z0-9_\-]{1,40}$")
    name: str = Field(max_length=120)


class DraftAssignment(BaseModel):
    file_id: str
    folder_ref: str = Field(pattern=r"^#[a-z0-9_\-]{1,40}$")


class DraftPlan(BaseModel):
    """Claude's raw organization suggestion, before deterministic compilation.

    Deliberately *not* a list of Operations. The model can only propose folder
    names and file-to-folder assignments; it has no way to express a rename, a
    delete, or a destination outside the workspace, because those shapes do not
    exist in this schema. Deterministic code compiles this into Operations with
    the root folder as the only possible parent.
    """

    folders: list[DraftFolder] = Field(default_factory=list, max_length=25)
    assignments: list[DraftAssignment] = Field(default_factory=list)
    rationale: str = ""


class OpResult(BaseModel):
    index: int
    summary: str
    status: Literal["executed", "skipped", "failed", "dry_run"]
    file_id: str | None = None
    detail: str | None = None


class ApplyResult(BaseModel):
    proposal_id: str
    dry_run: bool
    applied: bool
    results: list[OpResult]
    executed: int
    failed: int
    correlation_id: str
    approval: ApprovalStatus | None = Field(
        default=None, description="Approval state at the time of the call"
    )


# --------------------------------------------------------------------------
# Audit
# --------------------------------------------------------------------------


class AuditRow(BaseModel):
    id: int | None = None
    ts: datetime
    correlation_id: str
    actor: Literal["mcp_client", "cli", "system"]
    tool: str
    action: str
    outcome: Literal["ok", "refused", "error"]
    proposal_id: str | None = None
    reason_code: str | None = None
    approved_by: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)
