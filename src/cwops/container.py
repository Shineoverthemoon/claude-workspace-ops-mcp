"""Dependency wiring.

One place decides whether the server talks to real Google and real Claude or to
the offline fakes. Nothing below this module reads Settings to make that choice,
which is what keeps the safety-critical code testable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .ai.fake import FakeOrganizer
from .ai.protocol import Organizer
from .clock import Clock, SystemClock
from .config import Settings, load_settings
from .drive.fake import FakeDriveClient
from .drive.protocol import DriveClient
from .logging import get_logger, log_event
from .store import AuditLog, Database, GrantLedger, ProposalStore
from .workspace import Workspace

logger = get_logger("container")


@dataclass
class Container:
    settings: Settings
    clock: Clock
    db: Database
    audit: AuditLog
    grants: GrantLedger
    proposals: ProposalStore
    drive: DriveClient
    organizer: Organizer
    workspace: Workspace

    def close(self) -> None:
        self.db.close()


def build_drive(settings: Settings) -> DriveClient:
    if settings.drive_backend == "fake":
        return FakeDriveClient.from_seed()
    from .drive.client import GoogleDriveClient  # imported lazily: heavy deps

    return GoogleDriveClient.from_settings(settings)


def build_organizer(settings: Settings) -> Organizer:
    if settings.ai_backend == "fake":
        return FakeOrganizer()
    from .ai.claude import ClaudeOrganizer  # imported lazily: heavy deps

    return ClaudeOrganizer.from_settings(settings)


def build_container(
    settings: Settings | None = None,
    *,
    clock: Clock | None = None,
    drive: DriveClient | None = None,
    organizer: Organizer | None = None,
) -> Container:
    """Assemble the system. Overrides exist so tests inject fakes explicitly."""
    settings = settings or load_settings()
    clock = clock or SystemClock()
    db = Database(settings.db_path)
    drive = drive if drive is not None else build_drive(settings)
    organizer = organizer if organizer is not None else build_organizer(settings)
    grants = GrantLedger(db, clock)

    # Demo mode has a known root, so the offline walkthrough needs no setup step.
    root_folder_id = settings.root_folder_id
    if not root_folder_id and isinstance(drive, FakeDriveClient):
        root_folder_id = drive.root_folder_id

    container = Container(
        settings=settings,
        clock=clock,
        db=db,
        audit=AuditLog(db, clock),
        grants=grants,
        proposals=ProposalStore(db, clock, settings.approval_ttl_seconds),
        drive=drive,
        organizer=organizer,
        workspace=Workspace(
            drive=drive,
            grants=grants,
            root_folder_id=root_folder_id,
            max_files=settings.max_files_per_proposal * 4,
        ),
    )
    log_event(
        logger,
        logging.INFO,
        "container.ready",
        drive_backend=settings.drive_backend,
        ai_backend=settings.ai_backend,
        allow_mutations=settings.allow_mutations,
        root_folder_id=container.workspace.root_folder_id or None,
    )
    return container
