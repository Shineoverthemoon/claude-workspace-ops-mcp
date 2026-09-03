"""Plan validation - the boundary between "something proposed this" and
"the system is willing to do this".

Every plan reaching this module is treated as hostile input regardless of
origin. A plan produced by Claude and a plan typed by a user go through exactly
the same checks, because the model's output is data, never authority.

Invariants enforced here:

* I3 - every target resolves to a descendant of the app-owned root folder.
* I4 - every file ID must be one the *server itself* fetched for this plan;
       an ID the model invented has nothing to match against.
* I8 - every target must hold a recorded drive.file grant.

Rejected operations are dropped, never repaired, and always surfaced in the
proposal and the audit log so a human approves an explicit list.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..errors import LimitExceeded
from ..models import (
    CreateFolder,
    DriveFile,
    Move,
    Operation,
    RejectedOp,
    Rename,
    ValidationReport,
    is_ref,
    summarize_op,
)
from .naming import NameProblem, check_name

# Stable rejection reason codes. Asserted on in tests and written to the audit
# log, so they are part of the contract.
UNKNOWN_FILE = "unknown_file"
NOT_GRANTED = "not_granted"
OUTSIDE_WORKSPACE = "outside_workspace"
UNVERIFIABLE_PARENT = "unverifiable_parent"
INVALID_NAME = "invalid_name"
UNKNOWN_REF = "unknown_ref"
DUPLICATE_REF = "duplicate_ref"
DUPLICATE_TARGET = "duplicate_target"
NOT_A_FOLDER = "not_a_folder"
CYCLE = "cycle"
ROOT_IMMUTABLE = "root_immutable"

Problem = tuple[str, str]


@dataclass
class ValidationContext:
    """Everything the validator is allowed to know.

    ``known_files`` is the snapshot the server fetched while building the plan.
    Nothing outside it can be referenced, which is what makes a hallucinated
    file ID unusable rather than merely unlikely.
    """

    root_folder_id: str
    known_files: dict[str, DriveFile]
    granted_ids: set[str] = field(default_factory=set)
    max_ops: int = 50


def validate_plan(
    ops: list[Operation], ctx: ValidationContext
) -> tuple[list[Operation], ValidationReport]:
    """Return ``(accepted_ops, report)``. Order of accepted ops is preserved."""
    if len(ops) > ctx.max_ops:
        raise LimitExceeded(
            f"Plan has {len(ops)} operations; the limit is {ctx.max_ops}.",
            submitted=len(ops),
            limit=ctx.max_ops,
        )

    accepted: list[Operation] = []
    rejected: list[RejectedOp] = []
    declared_refs: dict[str, str] = {}
    touched: set[tuple[str, str]] = set()

    for index, op in enumerate(ops):
        problem = _check_op(op, ctx, declared_refs, touched)
        if problem is not None:
            reason_code, detail = problem
            rejected.append(
                RejectedOp(
                    index=index,
                    summary=summarize_op(op),
                    reason_code=reason_code,
                    detail=detail,
                )
            )
            continue

        accepted.append(op)
        if isinstance(op, CreateFolder):
            declared_refs[op.ref] = op.parent_id
        elif isinstance(op, Rename):
            touched.add(("rename", op.file_id))
        else:
            touched.add(("move", op.file_id))

    report = ValidationReport(ok=bool(accepted), accepted=len(accepted), rejected=rejected)
    return accepted, report


# ---------------------------------------------------------------------------
# Per-operation checks
# ---------------------------------------------------------------------------


def _check_op(
    op: Operation,
    ctx: ValidationContext,
    declared_refs: dict[str, str],
    touched: set[tuple[str, str]],
) -> Problem | None:
    if isinstance(op, CreateFolder):
        return _check_create_folder(op, ctx, declared_refs)
    if isinstance(op, Rename):
        return _check_rename(op, ctx, touched)
    return _check_move(op, ctx, declared_refs, touched)


def _check_create_folder(
    op: CreateFolder, ctx: ValidationContext, declared_refs: dict[str, str]
) -> Problem | None:
    if op.ref in declared_refs:
        return DUPLICATE_REF, f"Ref {op.ref} is declared more than once in this plan."
    name_problem = check_name(op.name)
    if name_problem is not NameProblem.OK:
        return INVALID_NAME, f"Folder name rejected: {name_problem.value}."
    return _check_destination(op.parent_id, ctx, declared_refs)


def _check_rename(
    op: Rename, ctx: ValidationContext, touched: set[tuple[str, str]]
) -> Problem | None:
    if ("rename", op.file_id) in touched:
        return DUPLICATE_TARGET, f"{op.file_id} is renamed more than once in this plan."
    name_problem = check_name(op.new_name)
    if name_problem is not NameProblem.OK:
        return INVALID_NAME, f"New name rejected: {name_problem.value}."
    return _check_source(op.file_id, ctx)


def _check_move(
    op: Move,
    ctx: ValidationContext,
    declared_refs: dict[str, str],
    touched: set[tuple[str, str]],
) -> Problem | None:
    if ("move", op.file_id) in touched:
        return DUPLICATE_TARGET, f"{op.file_id} is moved more than once in this plan."
    return (
        _check_source(op.file_id, ctx)
        or _check_destination(op.new_parent_id, ctx, declared_refs)
        or _check_no_cycle(op, ctx, declared_refs)
    )


# ---------------------------------------------------------------------------
# Shared target checks
# ---------------------------------------------------------------------------


def _check_source(file_id: str, ctx: ValidationContext) -> Problem | None:
    """A file this plan wants to modify: known, granted, inside the workspace."""
    if file_id == ctx.root_folder_id:
        return ROOT_IMMUTABLE, "The workspace root itself cannot be renamed or moved."
    if file_id not in ctx.known_files:
        return (
            UNKNOWN_FILE,
            f"{file_id} was not among the files this plan was built from "
            "(it may have been invented).",
        )
    if file_id not in ctx.granted_ids:
        return NOT_GRANTED, f"No drive.file grant is recorded for {file_id}."
    return _check_containment(file_id, ctx)


def _check_destination(
    parent_id: str, ctx: ValidationContext, declared_refs: dict[str, str]
) -> Problem | None:
    """A destination folder: either a ref declared earlier, or a real folder."""
    if is_ref(parent_id):
        if parent_id not in declared_refs:
            return (
                UNKNOWN_REF,
                f"{parent_id} is not created by an earlier operation in this plan.",
            )
        return None
    if parent_id not in ctx.known_files:
        return UNKNOWN_FILE, f"Destination {parent_id} is not a known file."
    if parent_id not in ctx.granted_ids:
        return NOT_GRANTED, f"No drive.file grant is recorded for destination {parent_id}."
    if not ctx.known_files[parent_id].is_folder:
        return NOT_A_FOLDER, f"Destination {parent_id} is not a folder."
    return _check_containment(parent_id, ctx)


def _check_containment(file_id: str, ctx: ValidationContext) -> Problem | None:
    """Fail closed: containment must be *proved*, not merely not disproved."""
    if file_id == ctx.root_folder_id:
        return None
    chain = _ancestor_chain(file_id, ctx.known_files)
    if ctx.root_folder_id in chain.ancestors:
        return None
    if not chain.complete:
        return (
            UNVERIFIABLE_PARENT,
            f"Parent chain of {file_id} leaves the known file set; containment "
            "in the workspace cannot be proved.",
        )
    return (
        OUTSIDE_WORKSPACE,
        f"{file_id} is not inside the managed workspace {ctx.root_folder_id}.",
    )


@dataclass
class _Chain:
    ancestors: list[str]
    complete: bool


def _ancestor_chain(file_id: str, known: dict[str, DriveFile]) -> _Chain:
    """Walk parents upward.

    ``complete`` is True only when the walk terminated at a parentless file that
    is itself present in ``known`` - i.e. the whole chain was observable.
    """
    ancestors: list[str] = []
    seen: set[str] = {file_id}
    current = known.get(file_id)
    if current is None:
        return _Chain(ancestors=[], complete=False)
    while current.parents:
        parent_id = current.parents[0]
        if parent_id in seen:  # defensive: Drive should never report a cycle
            return _Chain(ancestors=ancestors, complete=False)
        seen.add(parent_id)
        ancestors.append(parent_id)
        parent = known.get(parent_id)
        if parent is None:
            return _Chain(ancestors=ancestors, complete=False)
        current = parent
    return _Chain(ancestors=ancestors, complete=True)


def _check_no_cycle(
    op: Move, ctx: ValidationContext, declared_refs: dict[str, str]
) -> Problem | None:
    """Refuse moving a folder into itself or into one of its own descendants."""
    source = ctx.known_files.get(op.file_id)
    if source is None or not source.is_folder:
        return None
    anchor = _resolve_real_anchor(op.new_parent_id, declared_refs)
    if anchor is None:
        return None
    if anchor == op.file_id:
        return CYCLE, f"Cannot move folder {op.file_id} into itself."
    if op.file_id in _ancestor_chain(anchor, ctx.known_files).ancestors:
        return CYCLE, f"Cannot move folder {op.file_id} into its own descendant {anchor}."
    return None


def _resolve_real_anchor(target: str, declared_refs: dict[str, str]) -> str | None:
    """Follow a chain of plan-local refs down to the real folder it hangs from."""
    current = target
    for _ in range(len(declared_refs) + 1):
        if not is_ref(current):
            return current
        parent = declared_refs.get(current)
        if parent is None:
            return None
        current = parent
    return None


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------


def render_preview(ops: list[Operation], known: dict[str, DriveFile]) -> list[str]:
    """Human-readable diff shown before approval. Names, not just opaque IDs."""
    lines: list[str] = []
    ref_names: dict[str, str] = {}
    for op in ops:
        if isinstance(op, CreateFolder):
            ref_names[op.ref] = op.name
            lines.append(
                f"CREATE FOLDER  {op.name!r}  in  {_label(op.parent_id, known, ref_names)}"
            )
        elif isinstance(op, Rename):
            lines.append(
                f"RENAME         {_name_of(op.file_id, known)!r}  ->  {op.new_name!r}"
            )
        else:
            lines.append(
                f"MOVE           {_name_of(op.file_id, known)!r}  "
                f"from  {_source_label(op.file_id, known, ref_names)}  "
                f"->  {_label(op.new_parent_id, known, ref_names)}"
            )
    return lines


def _name_of(file_id: str, known: dict[str, DriveFile]) -> str:
    file = known.get(file_id)
    return file.name if file else file_id


def _label(target: str, known: dict[str, DriveFile], ref_names: dict[str, str]) -> str:
    if is_ref(target):
        return f"{ref_names.get(target, target)!r} (new)"
    file = known.get(target)
    return repr(file.name) if file else target


def _source_label(file_id: str, known: dict[str, DriveFile], ref_names: dict[str, str]) -> str:
    file = known.get(file_id)
    if file is None or not file.parents:
        return "?"
    return _label(file.parents[0], known, ref_names)
