"""The DriveClient contract.

Deliberately narrow: seven methods, four read and three write. A small surface
is what makes "exactly one write path" auditable - see MUTATING_METHODS, which
the dry-run test asserts against.

Under the drive.file scope an implementation can only see files the app created
or that the user explicitly opened/shared with it. Requests for anything else
fail as if the file did not exist, so implementations raise DriveApiError with
status 404 rather than leaking existence.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..models import DriveFile

#: Methods that change Drive state. Kept here so tests can assert none were
#: called during a read-only or dry-run flow without hard-coding names.
MUTATING_METHODS = frozenset({"create_folder", "rename", "move"})


@runtime_checkable
class DriveClient(Protocol):
    # -- read ---------------------------------------------------------------

    def get_file(self, file_id: str) -> DriveFile:
        """Fetch one file's metadata. Raises DriveApiError(404) if not granted."""
        ...

    def list_children(self, folder_id: str, page_size: int = 100) -> list[DriveFile]:
        """Direct children of a folder, excluding trashed files."""
        ...

    def search(
        self, query: str, folder_id: str | None = None, page_size: int = 25
    ) -> list[DriveFile]:
        """Name search across the granted set, optionally scoped to a folder."""
        ...

    def export_text(self, file_id: str, max_chars: int) -> tuple[str, bool]:
        """Return ``(text, truncated)``. Binary formats return an empty string."""
        ...

    # -- write --------------------------------------------------------------

    def create_folder(self, name: str, parent_id: str) -> DriveFile:
        """Create a folder. The app authors it, so a drive.file grant follows."""
        ...

    def rename(self, file_id: str, new_name: str) -> DriveFile: ...

    def move(
        self, file_id: str, new_parent_id: str, expected_old_parent_id: str | None = None
    ) -> DriveFile: ...
