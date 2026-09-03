"""Workspace provisioning: the bootstrap that makes drive.file usable.

Creating a folder is what *grants* access to it, so this is where the app's
reachable set begins. It is CLI-only and never exposed as a tool.
"""

from __future__ import annotations

import pytest

from cwops.container import Container
from cwops.drive import FakeDriveClient
from cwops.errors import ConfigurationError
from cwops.models import GrantSource
from cwops.provision import init_workspace, seed_workspace


def test_init_creates_an_app_owned_folder_and_records_the_grant(
    container: Container, drive: FakeDriveClient
) -> None:
    folder = init_workspace(container)

    assert folder.is_folder
    assert folder.name == "Claude Workspace Ops"
    grant = container.grants.require(folder.id)
    assert grant.source is GrantSource.APP_CREATED
    assert [call.method for call in drive.mutating_calls] == ["create_folder"]


def test_init_is_audited(container: Container) -> None:
    folder = init_workspace(container)
    rows = [row for row in container.audit.recent() if row.tool == "cwops_workspace_init"]
    assert rows and rows[0].detail["folder_id"] == folder.id
    assert rows[0].actor == "cli"


def test_seeding_requires_the_google_backend(container: Container) -> None:
    """The fake backend already ships a populated workspace; refuse, don't fake it."""
    with pytest.raises(ConfigurationError) as exc:
        seed_workspace(container, "root-workspace")
    assert "Google backend" in exc.value.detail


def test_seed_creates_files_and_grants_when_supported(container: Container) -> None:
    created: list[tuple[str, str, str]] = []

    def create_text_file(name: str, parent_id: str, content: str):  # type: ignore[no-untyped-def]
        created.append((name, parent_id, content))
        return container.drive.create_folder(name, parent_id)  # stand-in object

    container.drive.create_text_file = create_text_file  # type: ignore[attr-defined]
    files = seed_workspace(container, "root-workspace")

    assert len(files) == len(created) >= 5
    assert all(parent == "root-workspace" for _, parent, _ in created)
    # The seed deliberately contains an exact duplicate pair to demo detection.
    contents = [content for _, _, content in created]
    assert len(contents) != len(set(contents))
    assert all(container.grants.is_granted(file.id) for file in files)
