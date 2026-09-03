"""In-memory DriveClient used by the test suite and by demo mode.

It emulates the drive.file boundary faithfully: a file outside the grant set
raises 404 rather than 403, because that is what Google does - the scope hides
existence rather than reporting a permission denial. Tests that assert on
least-privilege behaviour are therefore testing the real contract.

It also records every call, so a test can assert that a read-only or dry-run
flow performed zero mutations.
"""

from __future__ import annotations

import json
from collections import deque
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..errors import DriveApiError
from ..models import FOLDER_MIME, DriveFile
from .protocol import MUTATING_METHODS

SEED_PATH = Path(__file__).with_name("seed_data.json")

#: MIME types the fake can produce text for, mirroring what the real client
#: exports. Everything else yields an empty string rather than binary noise.
TEXT_EXPORTABLE = {
    "text/plain",
    "text/markdown",
    "text/csv",
    "application/vnd.google-apps.document",
    "application/vnd.google-apps.spreadsheet",
    "application/vnd.google-apps.presentation",
}


@dataclass
class RecordedCall:
    method: str
    args: dict[str, Any] = field(default_factory=dict)


class FakeDriveClient:
    """Deterministic, offline DriveClient implementation."""

    def __init__(
        self,
        files: dict[str, DriveFile],
        contents: dict[str, str] | None = None,
        granted: set[str] | None = None,
        root_folder_id: str = "root-workspace",
    ) -> None:
        self._files = files
        self._contents = contents or {}
        self._granted = granted if granted is not None else set(files)
        self.root_folder_id = root_folder_id
        self.calls: list[RecordedCall] = []
        self._errors: dict[tuple[str, str | None], deque[Exception]] = {}
        self._next_id = 1

    # -- construction -------------------------------------------------------

    @classmethod
    def from_seed(cls, path: Path | None = None) -> FakeDriveClient:
        raw = json.loads((path or SEED_PATH).read_text(encoding="utf-8"))
        files = {
            entry["id"]: DriveFile.model_validate(entry)
            for entry in raw["files"]
            if not entry.get("_comment")
        }
        return cls(
            files=files,
            contents=raw.get("contents", {}),
            granted=set(raw["granted"]),
            root_folder_id=raw.get("root_folder_id", "root-workspace"),
        )

    # -- test controls ------------------------------------------------------

    def queue_error(self, method: str, error: Exception, file_id: str | None = None) -> None:
        """Make the next matching call raise ``error``. Used by the retry tests."""
        self._errors.setdefault((method, file_id), deque()).append(error)

    def grant(self, file_id: str) -> None:
        self._granted.add(file_id)

    def revoke(self, file_id: str) -> None:
        self._granted.discard(file_id)

    @property
    def mutating_calls(self) -> list[RecordedCall]:
        return [call for call in self.calls if call.method in MUTATING_METHODS]

    def snapshot(self) -> dict[str, DriveFile]:
        return deepcopy(self._files)

    # -- internals ----------------------------------------------------------

    def _record(self, method: str, **args: Any) -> None:
        self.calls.append(RecordedCall(method=method, args=args))

    def _maybe_raise(self, method: str, file_id: str | None = None) -> None:
        for key in ((method, file_id), (method, None)):
            queued = self._errors.get(key)
            if queued:
                raise queued.popleft()

    def _visible(self, file_id: str) -> DriveFile:
        """Return a file, or raise 404 exactly as the drive.file scope would."""
        file = self._files.get(file_id)
        if file is None or file_id not in self._granted:
            raise DriveApiError(
                f"File not found: {file_id}",
                status=404,
                file_id=file_id,
                hint="Outside the drive.file grant, or does not exist.",
            )
        return file

    # -- read ---------------------------------------------------------------

    def get_file(self, file_id: str) -> DriveFile:
        self._record("get_file", file_id=file_id)
        self._maybe_raise("get_file", file_id)
        return self._visible(file_id).model_copy(deep=True)

    def list_children(self, folder_id: str, page_size: int = 100) -> list[DriveFile]:
        self._record("list_children", folder_id=folder_id, page_size=page_size)
        self._maybe_raise("list_children", folder_id)
        parent = self._visible(folder_id)
        if not parent.is_folder:
            raise DriveApiError(f"Not a folder: {folder_id}", status=400, file_id=folder_id)
        children = [
            file.model_copy(deep=True)
            for file in self._files.values()
            if folder_id in file.parents and file.id in self._granted and not file.trashed
        ]
        children.sort(key=lambda f: (not f.is_folder, f.name.lower()))
        return children[:page_size]

    def search(
        self, query: str, folder_id: str | None = None, page_size: int = 25
    ) -> list[DriveFile]:
        self._record("search", query=query, folder_id=folder_id, page_size=page_size)
        self._maybe_raise("search", folder_id)
        needle = query.strip().lower()
        matches = [
            file.model_copy(deep=True)
            for file in self._files.values()
            if file.id in self._granted
            and not file.trashed
            and (not needle or needle in file.name.lower())
            and (folder_id is None or folder_id in file.parents)
        ]
        matches.sort(key=lambda f: f.name.lower())
        return matches[:page_size]

    def export_text(self, file_id: str, max_chars: int) -> tuple[str, bool]:
        self._record("export_text", file_id=file_id, max_chars=max_chars)
        self._maybe_raise("export_text", file_id)
        file = self._visible(file_id)
        if file.mime_type not in TEXT_EXPORTABLE:
            return "", False
        text = self._contents.get(file_id, "")
        if len(text) > max_chars:
            return text[:max_chars], True
        return text, False

    # -- write --------------------------------------------------------------

    def create_folder(self, name: str, parent_id: str) -> DriveFile:
        self._record("create_folder", name=name, parent_id=parent_id)
        self._maybe_raise("create_folder", parent_id)
        # "root" is Drive's alias for My Drive. Creating there is always allowed
        # under drive.file - it is how the app bootstraps its own workspace.
        if parent_id != "root":
            self._visible(parent_id)
        new_id = f"gen-{self._next_id}"
        self._next_id += 1
        folder = DriveFile(id=new_id, name=name, mime_type=FOLDER_MIME, parents=[parent_id])
        self._files[new_id] = folder
        # App-authored, so the drive.file grant follows automatically.
        self._granted.add(new_id)
        return folder.model_copy(deep=True)

    def rename(self, file_id: str, new_name: str) -> DriveFile:
        self._record("rename", file_id=file_id, new_name=new_name)
        self._maybe_raise("rename", file_id)
        file = self._visible(file_id)
        file.name = new_name
        return file.model_copy(deep=True)

    def move(
        self, file_id: str, new_parent_id: str, expected_old_parent_id: str | None = None
    ) -> DriveFile:
        self._record(
            "move",
            file_id=file_id,
            new_parent_id=new_parent_id,
            expected_old_parent_id=expected_old_parent_id,
        )
        self._maybe_raise("move", file_id)
        file = self._visible(file_id)
        self._visible(new_parent_id)
        if expected_old_parent_id is not None and expected_old_parent_id not in file.parents:
            raise DriveApiError(
                f"{file_id} is no longer in {expected_old_parent_id}; it moved since planning.",
                status=409,
                file_id=file_id,
            )
        file.parents = [new_parent_id]
        return file.model_copy(deep=True)
