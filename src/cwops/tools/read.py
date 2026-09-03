"""Read-only tools. None of these can change Drive.

Every one is bounded by the drive.file grant: Google returns only files this app
created or that the user explicitly opened/shared with it, so there is no query
here that can reach the rest of the user's Drive.
"""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from ..container import Container
from ..models import DuplicateReport, FileContent, SearchResult
from ..rules import find_duplicates
from .base import audited


def register(server: MCPServer, container: Container) -> None:
    settings = container.settings

    @server.tool(
        name="drive_search",
        description=(
            "Search files by name within this app's Drive access. Only files the "
            "app created or that the user explicitly opened/shared with it are "
            "visible - the rest of the user's Drive is unreachable by design. "
            "Read-only."
        ),
    )
    def drive_search(
        query: str,
        folder_id: str | None = None,
        max_results: int = 25,
    ) -> SearchResult:
        with audited(container, "drive_search", "search") as ctx:
            files, truncated = container.workspace.search(query, folder_id, max_results)
            ctx.note(query=query, folder_id=folder_id, results=len(files))
            return SearchResult(
                files=files,
                truncated=truncated,
                note="Scope is limited to this app's drive.file grant.",
            )

    @server.tool(
        name="drive_list_folder",
        description=(
            "List the contents of a folder. Set recursive=true to walk "
            "subfolders breadth-first. Trashed files are excluded. Read-only."
        ),
    )
    def drive_list_folder(
        folder_id: str,
        recursive: bool = False,
        max_results: int = 100,
    ) -> SearchResult:
        with audited(container, "drive_list_folder", "list") as ctx:
            files, truncated = container.workspace.list_folder(
                folder_id, recursive=recursive, limit=max_results
            )
            ctx.note(folder_id=folder_id, recursive=recursive, results=len(files))
            return SearchResult(files=files, truncated=truncated)

    @server.tool(
        name="drive_read_file",
        description=(
            "Read a file's text. Google Docs/Sheets/Slides are exported as plain "
            "text; binary formats return an empty string rather than noise. "
            "Output is truncated to the configured character cap. Read-only."
        ),
    )
    def drive_read_file(file_id: str, max_chars: int = 20000) -> FileContent:
        with audited(container, "drive_read_file", "read") as ctx:
            cap = min(max_chars, settings.max_file_chars)
            file, text, truncated = container.workspace.read_text(file_id, cap)
            ctx.note(file_id=file_id, chars=len(text), truncated=truncated)
            return FileContent(
                file_id=file.id,
                name=file.name,
                mime_type=file.mime_type,
                text=text,
                truncated=truncated,
                chars=len(text),
            )

    @server.tool(
        name="find_duplicates",
        description=(
            "Find duplicate files in a folder. Fully deterministic - no model is "
            "consulted. strategy='checksum' matches Drive's md5Checksum exactly; "
            "Google-native Docs/Sheets have no checksum and are reported as "
            "'unscannable' rather than assumed unique. strategy='metadata' "
            "compares normalized name, size and type, and is always labelled "
            "heuristic. Read-only."
        ),
    )
    def find_duplicate_files(
        folder_id: str,
        strategy: str = "checksum",
        recursive: bool = False,
    ) -> DuplicateReport:
        with audited(container, "find_duplicates", "find_duplicates") as ctx:
            chosen: str = strategy if strategy in ("checksum", "metadata") else "checksum"
            files, _ = container.workspace.list_folder(
                folder_id, recursive=recursive, limit=settings.max_files_per_proposal * 4
            )
            report = find_duplicates(files, chosen)  # type: ignore[arg-type]
            ctx.note(
                folder_id=folder_id,
                strategy=chosen,
                scanned=report.scanned,
                groups=len(report.groups),
                unscannable=len(report.unscannable),
            )
            return report
