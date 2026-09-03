"""Workspace provisioning. Human-initiated, CLI-only, never an MCP tool.

This is the second and last place in the codebase that mutates Drive, and it
exists because drive.file access has to start somewhere: the app creates a
folder, and creating it is what grants access to it. From then on, everything
inside that folder is reachable and everything outside it is not.

Called only from cwops.cli, in the operator's trust zone. The agent-facing MCP
surface has no provisioning capability.
"""

from __future__ import annotations

from .config import WORKSPACE_FOLDER_NAME
from .container import Container
from .errors import ConfigurationError
from .models import DriveFile, GrantSource

SEED_FILES: tuple[tuple[str, str], ...] = (
    ("Invoice Q1 2026.txt", "Invoice 0001. Amount due: 1,240.00. Vendor: Acme Supplies."),
    (
        "Copy of Invoice Q1 2026.txt",
        "Invoice 0001. Amount due: 1,240.00. Vendor: Acme Supplies.",
    ),
    ("Invoice Q2 2026.txt", "Invoice 0002. Amount due: 980.00. Vendor: Acme Supplies."),
    ("Vendor Contract - Acme.txt", "Master services agreement with Acme Supplies, 2026."),
    ("Meeting Notes 2026-01-14.txt", "Weekly sync. Topics: invoicing backlog, renewals."),
    ("Tax Return 2025.txt", "Filed 2026-04-14. Reference 55-2025."),
)


def init_workspace(container: Container) -> DriveFile:
    """Create the app-owned root folder and record the resulting grant."""
    folder = container.drive.create_folder(WORKSPACE_FOLDER_NAME, "root")
    container.grants.record(folder, GrantSource.APP_CREATED)
    container.audit.record(
        actor="cli",
        tool="cwops_workspace_init",
        action="create_workspace",
        outcome="ok",
        detail={"folder_id": folder.id, "name": folder.name},
    )
    return folder


def seed_workspace(container: Container, root_folder_id: str) -> list[DriveFile]:
    """Populate the workspace with demo files, including exact duplicates."""
    creator = getattr(container.drive, "create_text_file", None)
    if creator is None:
        raise ConfigurationError(
            "Seeding requires the Google backend. The fake backend already ships "
            "a populated demo workspace.",
            drive_backend=container.settings.drive_backend,
        )
    created: list[DriveFile] = []
    for name, content in SEED_FILES:
        file = creator(name, root_folder_id, content)
        container.grants.record(file, GrantSource.APP_CREATED)
        created.append(file)
    container.audit.record(
        actor="cli",
        tool="cwops_workspace_seed",
        action="seed_workspace",
        outcome="ok",
        detail={"root_folder_id": root_folder_id, "created": len(created)},
    )
    return created
