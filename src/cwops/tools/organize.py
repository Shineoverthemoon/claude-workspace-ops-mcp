"""Proposal-producing tools. None of these can change Drive either.

``propose_organization`` is the one place a language model influences a plan.
What it returns is a DraftPlan - folder names and file assignments - which
deterministic code compiles into Operations whose parent is always the workspace
root. The model cannot express a rename, a delete, or a destination outside the
workspace, because the schema it answers in has no way to say those things.
"""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from ..container import Container
from ..errors import DriveApiError, LimitExceeded
from ..models import (
    Classification,
    CreateFolder,
    DraftPlan,
    DriveFile,
    Move,
    Operation,
    Proposal,
    ProposalView,
    compute_plan_hash,
)
from ..rules import ValidationContext, render_preview, validate_plan
from ..store.proposals import new_proposal_id
from .base import audited


def _snippets(container: Container, files: list[DriveFile]) -> dict[str, str]:
    """Fetch a bounded text sample per file. Content is untrusted input."""
    cap = container.settings.max_file_chars
    per_file = max(400, cap // max(len(files), 1))
    out: dict[str, str] = {}
    for file in files:
        if file.is_folder:
            continue
        try:
            text, _ = container.drive.export_text(file.id, per_file)
        except DriveApiError:
            continue
        if text:
            out[file.id] = text
    return out


def _compile_draft(
    draft: DraftPlan, candidates: list[DriveFile], root_folder_id: str
) -> list[Operation]:
    """Turn a model DraftPlan into Operations. Deterministic, and narrowing.

    Every created folder is parented to the workspace root - that value comes
    from configuration, never from the model. Assignments referencing unknown
    files or refs are compiled faithfully so the validator rejects them
    visibly, rather than being silently dropped here.
    """
    by_id = {file.id: file for file in candidates}
    ops: list[Operation] = [
        CreateFolder(ref=folder.ref, name=folder.name, parent_id=root_folder_id)
        for folder in draft.folders
    ]
    for assignment in draft.assignments:
        current = by_id.get(assignment.file_id)
        ops.append(
            Move(
                file_id=assignment.file_id,
                new_parent_id=assignment.folder_ref,
                expected_old_parent_id=(
                    current.parents[0] if current and current.parents else None
                ),
            )
        )
    return ops


def _build_proposal(
    container: Container,
    kind: str,
    ops: list[Operation],
    known: dict[str, DriveFile],
    notes: str | None = None,
) -> Proposal:
    root = container.workspace.root_folder_id
    ctx = ValidationContext(
        root_folder_id=root,
        known_files=known,
        granted_ids=container.grants.granted_ids(),
        max_ops=container.settings.max_files_per_proposal,
    )
    accepted, report = validate_plan(ops, ctx)
    proposal = Proposal(
        id=new_proposal_id(),
        created_at=container.clock.now(),
        kind=kind,  # type: ignore[arg-type]
        root_folder_id=root,
        ops=accepted,
        plan_hash=compute_plan_hash(root, accepted),
        preview=render_preview(accepted, known),
        validation=report,
        granted_file_ids=sorted(known),
        notes=notes,
    )
    return container.proposals.create(proposal)


def _resolve_targets(container: Container, ops: list[Operation]) -> dict[str, DriveFile]:
    """Fetch every file an operation names, so the validator has real metadata.

    A file that cannot be fetched is simply absent from the snapshot, and the
    validator then rejects the operation that referenced it.
    """
    wanted: set[str] = set()
    for op in ops:
        if isinstance(op, CreateFolder):
            wanted.add(op.parent_id)
        elif isinstance(op, Move):
            wanted.update({op.file_id, op.new_parent_id})
        else:
            wanted.add(op.file_id)

    found: list[DriveFile] = []
    for file_id in sorted(wanted):
        if file_id.startswith("#"):
            continue
        try:
            found.append(container.workspace.get(file_id))
        except DriveApiError:
            continue
    return container.workspace.snapshot(found)


def register(server: MCPServer, container: Container) -> None:
    settings = container.settings

    @server.tool(
        name="classify_documents",
        description=(
            "Ask Claude to categorize documents whose purpose is ambiguous from "
            "metadata alone. Advisory only: the result is a label with a "
            "confidence score and cannot authorize any action. File IDs the "
            "model returns that were not in the request are discarded."
        ),
    )
    def classify_documents(
        file_ids: list[str], include_content: bool = True
    ) -> list[Classification]:
        with audited(container, "classify_documents", "classify") as ctx:
            if len(file_ids) > settings.max_files_per_proposal:
                raise LimitExceeded(
                    f"{len(file_ids)} files requested; limit is "
                    f"{settings.max_files_per_proposal}.",
                    submitted=len(file_ids),
                    limit=settings.max_files_per_proposal,
                )
            files = [container.workspace.get(file_id) for file_id in file_ids]
            snippets = _snippets(container, files) if include_content else {}
            results = container.organizer.classify(files, snippets)

            # The model may only speak about files we asked about.
            requested = set(file_ids)
            kept = [item for item in results if item.file_id in requested]
            ctx.note(
                requested=len(file_ids),
                returned=len(results),
                kept=len(kept),
                with_content=len(snippets),
            )
            return kept

    @server.tool(
        name="propose_organization",
        description=(
            "Produce a validated, human-approvable plan for tidying a folder. "
            "Claude suggests folder names and file assignments; deterministic "
            "code compiles them into operations rooted at the managed workspace, "
            "validates every one, and stores the result. NOTHING IS CHANGED - "
            "call apply_proposal after a human runs `cwops approve <id>`."
        ),
    )
    def propose_organization(
        folder_id: str,
        instructions: str | None = None,
        max_files: int = 50,
    ) -> Proposal:
        with audited(container, "propose_organization", "propose") as ctx:
            cap = min(max_files, settings.max_files_per_proposal)
            listed, truncated = container.workspace.list_folder(folder_id, limit=cap)
            candidates = [file for file in listed if not file.is_folder]

            classifications = container.organizer.classify(
                candidates, _snippets(container, candidates)
            )
            draft = container.organizer.suggest_taxonomy(
                candidates, classifications, instructions
            )
            ops = _compile_draft(draft, candidates, container.workspace.root_folder_id)
            known = container.workspace.snapshot(listed)

            proposal = _build_proposal(
                container,
                "organization",
                ops,
                known,
                notes=draft.rationale[:500] or None,
            )
            ctx.proposal_id = proposal.id
            ctx.note(
                folder_id=folder_id,
                candidates=len(candidates),
                truncated=truncated,
                proposed=len(ops),
                accepted=proposal.validation.accepted,
                rejected=len(proposal.validation.rejected),
            )
            return proposal

    @server.tool(
        name="propose_operations",
        description=(
            "Build a proposal from explicit operations - create_folder, rename, "
            "move. No model is involved. Every operation is validated against "
            "the workspace boundary and the app's per-file Drive grants. "
            "NOTHING IS CHANGED until a human approves and apply_proposal runs."
        ),
    )
    def propose_operations(ops: list[Operation]) -> Proposal:
        with audited(container, "propose_operations", "propose") as ctx:
            known = _resolve_targets(container, ops)
            proposal = _build_proposal(container, "manual", ops, known)
            ctx.proposal_id = proposal.id
            ctx.note(
                submitted=len(ops),
                accepted=proposal.validation.accepted,
                rejected=len(proposal.validation.rejected),
            )
            return proposal

    @server.tool(
        name="get_proposal",
        description=(
            "Fetch a stored proposal: its operations, the human-readable preview, "
            "the validation report including every rejected operation, and its "
            "live approval state."
        ),
    )
    def get_proposal(proposal_id: str) -> ProposalView:
        with audited(container, "get_proposal", "get") as ctx:
            proposal = container.proposals.get(proposal_id)
            ctx.proposal_id = proposal.id
            approval = container.proposals.approval_status(proposal)
            ctx.note(status=proposal.status.value, approved=approval.approved)
            return ProposalView(proposal=proposal, approval=approval)
