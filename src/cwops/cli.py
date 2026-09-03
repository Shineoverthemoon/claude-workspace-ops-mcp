"""Operator CLI - the human half of the system.

`cwops approve` is deliberately the ONLY way an approval record comes into
existence. It is not exposed as an MCP tool and takes no input from the model,
so approval lives in a different process and a different trust zone from the
agent that wants it. That separation is the whole security argument: a model
can ask for approval, but it has no mechanism to produce one.

Unlike the MCP server, this is an ordinary CLI and prints to stdout.
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path
from typing import Any

from .config import load_settings
from .container import Container, build_container
from .errors import CwopsError
from .logging import configure_logging, new_correlation_id, set_correlation_id
from .models import ProposalStatus


def _default_approver() -> str:
    try:
        return getpass.getuser()
    except Exception:  # pragma: no cover - platform dependent
        return "unknown"


def _open(args: argparse.Namespace) -> Container:
    settings = load_settings()
    configure_logging(settings.log_level)
    return build_container(settings)


# --- commands -------------------------------------------------------------


def cmd_approve(container: Container, args: argparse.Namespace) -> int:
    proposal = container.proposals.get(args.proposal_id)
    print(f"Proposal {proposal.id}  ({proposal.kind}, {len(proposal.ops)} operations)")
    print(f"Workspace root: {proposal.root_folder_id}")
    print()
    for line in proposal.preview:
        print(f"  {line}")
    if proposal.validation.rejected:
        print()
        print(f"  {len(proposal.validation.rejected)} operation(s) were REJECTED and will "
              "not run:")
        for rejection in proposal.validation.rejected:
            print(f"    - [{rejection.reason_code}] {rejection.summary}")
    print()

    if not args.yes:
        answer = input("Approve these operations? [y/N] ").strip().lower()
        if answer not in {"y", "yes"}:
            print("Not approved.")
            return 1

    approver = args.approver or _default_approver()
    approval = container.proposals.approve(proposal.id, approver)
    container.audit.record(
        actor="cli",
        tool="cwops_approve",
        action="approve",
        outcome="ok",
        proposal_id=proposal.id,
        approved_by=approver,
        detail={"approval_id": approval.id, "expires_at": approval.expires_at.isoformat()},
    )
    print(f"Approved by {approver}. Single-use, expires {approval.expires_at.isoformat()}.")
    print(f"Now run apply_proposal(proposal_id='{proposal.id}', dry_run=false).")
    return 0


def cmd_list(container: Container, args: argparse.Namespace) -> int:
    status = ProposalStatus(args.status) if args.status else None
    proposals = container.proposals.list(status=status, limit=args.limit)
    if not proposals:
        print("No proposals.")
        return 0
    for proposal in proposals:
        approval = container.proposals.approval_status(proposal)
        flag = "approved" if approval.approved else "unapproved"
        print(
            f"{proposal.id}  {proposal.status.value:<8}  {flag:<10}  "
            f"{len(proposal.ops):>3} ops  {proposal.created_at.isoformat()}"
        )
    return 0


def cmd_show(container: Container, args: argparse.Namespace) -> int:
    proposal = container.proposals.get(args.proposal_id)
    approval = container.proposals.approval_status(proposal)
    print(json.dumps({"proposal": proposal.model_dump(mode="json"),
                      "approval": approval.model_dump(mode="json")}, indent=2))
    return 0


def cmd_audit(container: Container, args: argparse.Namespace) -> int:
    if args.export:
        written = container.audit.export_jsonl(Path(args.export))
        print(f"Exported {written} audit rows to {args.export}")
        return 0
    rows = container.audit.recent(limit=args.limit)
    for row in reversed(rows):
        approver = f"  approved_by={row.approved_by}" if row.approved_by else ""
        reason = f"  reason={row.reason_code}" if row.reason_code else ""
        print(
            f"{row.ts.isoformat()}  {row.correlation_id}  {row.actor:<10}  "
            f"{row.tool:<22} {row.outcome:<8}{reason}{approver}"
        )
    return 0


def cmd_auth(container: Container | None, args: argparse.Namespace) -> int:
    """Authorize with Google.

    Runs without building a container on purpose: with
    CWOPS_DRIVE_BACKEND=google the container construction itself requires
    working credentials, so bootstrapping auth through it would deadlock on
    the very first run.
    """
    from .drive.auth import load_credentials

    settings = load_settings()
    credentials = load_credentials(settings, allow_interactive=True)
    print(f"Authorized. Scopes granted: {credentials.scopes}")
    print(f"Token stored at {settings.token_path} (owner read/write only).")
    print(
        "\nThis is the drive.file scope: the app can see only files it creates "
        "or that you explicitly open/share with it."
    )
    return 0


def cmd_workspace(container: Container, args: argparse.Namespace) -> int:
    from .provision import init_workspace, seed_workspace

    if args.action == "init":
        folder = init_workspace(container)
        print(f"Created workspace folder: {folder.name}  ({folder.id})")
        print(f"\nAdd this to your .env:\n  CWOPS_ROOT_FOLDER_ID={folder.id}")
        return 0

    root = container.settings.require_root_folder()
    created = seed_workspace(container, root)
    print(f"Created {len(created)} demo files in {root}:")
    for file in created:
        print(f"  {file.id}  {file.name}")
    return 0


def cmd_demo(container: Container | None, args: argparse.Namespace) -> int:
    """Run the whole pipeline offline, narrating each safety gate.

    Uses the in-package fake Drive and the offline organizer, so it needs no
    Google account and no Anthropic key. Everything it exercises - validation,
    dry run, approval, audit - is the same code the real backends use.
    """
    import asyncio
    import tempfile

    from .ai import FakeOrganizer
    from .config import Settings
    from .drive import FakeDriveClient
    from .server import build_server

    drive = FakeDriveClient.from_seed()
    with tempfile.TemporaryDirectory() as tmp:
        settings = Settings(
            drive_backend="fake",
            ai_backend="fake",
            allow_mutations=True,
            root_folder_id=drive.root_folder_id,
            db_path=Path(tmp) / "demo.db",
            _env_file=None,  # type: ignore[call-arg]
        )
        demo = build_container(settings, drive=drive, organizer=FakeOrganizer())
        server = build_server(demo)

        def call(tool: str, **kwargs: object) -> Any:
            outcome = asyncio.run(server.call_tool(tool, kwargs))
            # These tools never request elicitation, so a result without
            # structured content means something is wrong with the wiring.
            payload = getattr(outcome, "structured_content", None)
            if payload is None:
                raise RuntimeError(f"{tool} returned no structured content")
            if isinstance(payload, dict) and set(payload) == {"result"}:
                return payload["result"]
            return payload

        def heading(text: str) -> None:
            print(f"\n{'=' * 72}\n{text}\n{'=' * 72}")

        try:
            heading("1. What can this app see?  (drive.file scope only)")
            listing = call("drive_list_folder", folder_id=drive.root_folder_id, recursive=True)
            for file in listing["files"]:
                print(f"  {file['id']:<22} {file['name']}")
            print(f"\n  {len(listing['files'])} files reachable.")
            print("  Files outside the app's grant are invisible - not merely forbidden.")

            heading("2. Deterministic duplicate detection  (no model involved)")
            report = call("find_duplicates", folder_id="fol-inbox", strategy="checksum")
            for group in report["groups"]:
                names = ", ".join(f["name"] for f in group["files"])
                print(f"  [{group['confidence']}] {names}")
            print(f"\n  {len(report['unscannable'])} file(s) have no checksum "
                  "(Google-native) and are reported as unscannable, not as unique.")

            heading("3. Propose an organization  (Claude advises, rules decide)")
            proposal = call("propose_organization", folder_id="fol-inbox")
            print(f"  Proposal {proposal['id']}")
            for line in proposal["preview"]:
                print(f"    {line}")
            print(f"\n  accepted={proposal['validation']['accepted']}  "
                  f"rejected={len(proposal['validation']['rejected'])}")
            print("  Nothing has been changed.")

            heading("4. Dry run  (the default)")
            dry = call("apply_proposal", proposal_id=proposal["id"])
            print(f"  dry_run={dry['dry_run']}  applied={dry['applied']}  "
                  f"approved={dry['approval']['approved']}")
            print(f"  Drive mutations so far: {len(drive.mutating_calls)}")

            heading("5. Real run WITHOUT approval  (must be refused)")
            try:
                call("apply_proposal", proposal_id=proposal["id"], dry_run=False)
                print("  UNEXPECTED: it was allowed.")
            except Exception as exc:
                print(f"  Refused: {exc}")
            print(f"  Drive mutations so far: {len(drive.mutating_calls)}")

            heading("6. Human approval  (CLI only - no tool can do this)")
            approval = demo.proposals.approve(proposal["id"], "demo-operator")
            demo.audit.record(
                actor="cli",
                tool="cwops_approve",
                action="approve",
                outcome="ok",
                proposal_id=proposal["id"],
                approved_by="demo-operator",
                detail={"approval_id": approval.id},
            )
            print(f"  Approved by demo-operator. Single-use, expires "
                  f"{approval.expires_at.isoformat()}.")

            heading("7. Real run WITH approval")
            applied = call("apply_proposal", proposal_id=proposal["id"], dry_run=False)
            print(f"  applied={applied['applied']}  executed={applied['executed']}  "
                  f"failed={applied['failed']}")
            for row in applied["results"]:
                print(f"    [{row['status']}] {row['summary']}")

            heading("8. Replay the same approval  (single-use)")
            try:
                call("apply_proposal", proposal_id=proposal["id"], dry_run=False)
                print("  UNEXPECTED: it was allowed.")
            except Exception as exc:
                print(f"  Refused: {exc}")

            heading("9. Audit trail")
            for row in demo.audit.recent(limit=100)[::-1]:
                approver = f"  approved_by={row.approved_by}" if row.approved_by else ""
                reason = f"  reason={row.reason_code}" if row.reason_code else ""
                print(f"  {row.correlation_id}  {row.actor:<10} {row.tool:<22} "
                      f"{row.outcome:<8}{reason}{approver}")
            print(f"\n  {demo.audit.count()} audit rows, one per tool call.")
            return 0
        finally:
            demo.close()


def cmd_grants(container: Container, args: argparse.Namespace) -> int:
    grants = container.grants.list_all()
    print(f"{len(grants)} file(s) reachable under the drive.file scope:")
    for grant in grants:
        print(f"  {grant.file_id:<24} {grant.source.value:<12} {grant.name}")
    return 0


def cmd_grant(container: Container, args: argparse.Namespace) -> int:
    """Verify and register access to a file.

    This does not *create* a grant - only the user can, by creating the file
    through this app or explicitly opening/sharing it with the app. It probes
    whether the grant already exists and records it if so.
    """
    file_id = args.file_id.strip().rstrip("/").split("/")[-1]
    try:
        file = container.workspace.get(file_id)
    except CwopsError as exc:
        print(f"No access to {file_id}: {exc.detail}", file=sys.stderr)
        print(
            "\nUnder the drive.file scope this app can only see files it created "
            "or that you explicitly opened/shared with it. Move the file into the "
            "app's workspace folder, or open it with this app, then retry.",
            file=sys.stderr,
        )
        return 1
    container.audit.record(
        actor="cli",
        tool="cwops_grant",
        action="verify_grant",
        outcome="ok",
        detail={"file_id": file.id, "name": file.name},
    )
    print(f"Verified access: {file.id}  {file.name}  ({file.mime_type})")
    return 0


# --- parser ---------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cwops",
        description="Operator CLI for Claude Workspace Ops MCP.",
    )
    parser.set_defaults(needs_container=True)
    sub = parser.add_subparsers(dest="command", required=True)

    approve = sub.add_parser("approve", help="Approve a proposal (human gate)")
    approve.add_argument("proposal_id")
    approve.add_argument("--approver", default=None, help="Defaults to the OS user")
    approve.add_argument("--yes", "-y", action="store_true", help="Skip the confirm prompt")
    approve.set_defaults(func=cmd_approve)

    listing = sub.add_parser("list", help="List proposals")
    listing.add_argument("--status", choices=[s.value for s in ProposalStatus])
    listing.add_argument("--limit", type=int, default=25)
    listing.set_defaults(func=cmd_list)

    show = sub.add_parser("show", help="Print a proposal as JSON")
    show.add_argument("proposal_id")
    show.set_defaults(func=cmd_show)

    audit = sub.add_parser("audit", help="Show or export the audit log")
    audit.add_argument("--limit", type=int, default=30)
    audit.add_argument("--export", metavar="PATH", help="Write the whole log as JSONL")
    audit.set_defaults(func=cmd_audit)

    demo = sub.add_parser("demo", help="Run the full pipeline offline, narrating each gate")
    # Self-contained: uses a temporary database so it never touches real state.
    demo.set_defaults(func=cmd_demo, needs_container=False)

    auth = sub.add_parser("auth", help="Authorize with Google (drive.file scope only)")
    # Must not need a container: the Google backend cannot be built until this
    # command has produced the credentials it would require.
    auth.set_defaults(func=cmd_auth, needs_container=False)

    workspace = sub.add_parser("workspace", help="Create or seed the app-owned workspace")
    workspace.add_argument("action", choices=["init", "seed"])
    workspace.set_defaults(func=cmd_workspace)

    grants = sub.add_parser("grants", help="List files this app can reach")
    grants.set_defaults(func=cmd_grants)

    grant = sub.add_parser("grant", help="Verify and register access to one file")
    grant.add_argument("file_id", help="Drive file ID or URL")
    grant.set_defaults(func=cmd_grant)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    container: Container | None = None
    # CLI actions are audited too, so they need a correlation ID like tool calls.
    set_correlation_id(new_correlation_id())
    try:
        if args.needs_container:
            container = _open(args)
        else:
            configure_logging(load_settings().log_level)
        result: int = args.func(container, args)
        return result
    except CwopsError as exc:
        print(f"[{exc.reason_code}] {exc.detail}", file=sys.stderr)
        return 2
    finally:
        if container is not None:
            container.close()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
