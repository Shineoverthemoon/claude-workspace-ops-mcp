"""Read-side workspace operations shared by the tool modules.

Also the place where the grant ledger stays honest: any file the Drive API
actually returned is, by definition, inside this app's drive.file grant, so it
is recorded as observed. The ledger is therefore a running record of reachable
files rather than a hand-maintained list.
"""

from __future__ import annotations

from collections import deque

from .drive.protocol import DriveClient
from .errors import DriveApiError
from .models import DriveFile, GrantSource
from .store import GrantLedger

MAX_RECURSION_DEPTH = 6


class Workspace:
    def __init__(
        self,
        drive: DriveClient,
        grants: GrantLedger,
        root_folder_id: str,
        max_files: int = 200,
    ) -> None:
        self._drive = drive
        self._grants = grants
        self.root_folder_id = root_folder_id
        self._max_files = max_files

    # -- grant bookkeeping --------------------------------------------------

    def observe(self, files: list[DriveFile], source: GrantSource = GrantSource.VERIFIED) -> None:
        """Record that these files are reachable under the current grant."""
        for file in files:
            self._grants.record(file, source)

    # -- reads --------------------------------------------------------------

    def get(self, file_id: str) -> DriveFile:
        file = self._drive.get_file(file_id)
        self.observe([file])
        return file

    def list_folder(
        self, folder_id: str, recursive: bool = False, limit: int = 100
    ) -> tuple[list[DriveFile], bool]:
        """Return ``(files, truncated)``. Breadth-first when recursive."""
        cap = min(limit, self._max_files)
        collected: list[DriveFile] = []
        queue: deque[tuple[str, int]] = deque([(folder_id, 0)])
        seen: set[str] = {folder_id}
        truncated = False

        while queue:
            current, depth = queue.popleft()
            children = self._drive.list_children(current, page_size=cap)
            for child in children:
                if len(collected) >= cap:
                    truncated = True
                    break
                collected.append(child)
                if (
                    recursive
                    and child.is_folder
                    and child.id not in seen
                    and depth + 1 < MAX_RECURSION_DEPTH
                ):
                    seen.add(child.id)
                    queue.append((child.id, depth + 1))
            if truncated:
                break

        self.observe(collected)
        return collected, truncated

    def search(
        self, query: str, folder_id: str | None = None, limit: int = 25
    ) -> tuple[list[DriveFile], bool]:
        cap = min(limit, self._max_files)
        results = self._drive.search(query, folder_id=folder_id, page_size=cap + 1)
        truncated = len(results) > cap
        results = results[:cap]
        self.observe(results)
        return results, truncated

    def read_text(self, file_id: str, max_chars: int) -> tuple[DriveFile, str, bool]:
        file = self.get(file_id)
        text, was_truncated = self._drive.export_text(file_id, max_chars)
        return file, text, was_truncated

    # -- plan context -------------------------------------------------------

    def snapshot(self, files: list[DriveFile]) -> dict[str, DriveFile]:
        """Build the known-file set a plan is validated against.

        Includes every ancestor up to the workspace root, so containment can be
        *proved* rather than assumed. An ancestor the app cannot see simply stays
        absent, and the validator then fails closed on that file.
        """
        known: dict[str, DriveFile] = {file.id: file for file in files}
        try:
            root = self.get(self.root_folder_id)
            known[root.id] = root
        except DriveApiError:
            pass  # Root unreachable: every containment check will fail closed.

        pending = [parent for file in files for parent in file.parents]
        visited: set[str] = set()
        while pending:
            parent_id = pending.pop()
            if parent_id in known or parent_id in visited:
                continue
            visited.add(parent_id)
            try:
                parent = self.get(parent_id)
            except DriveApiError:
                continue  # Outside the grant; containment stays unprovable.
            known[parent.id] = parent
            pending.extend(parent.parents)
        return known
