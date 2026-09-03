"""Google Drive implementation of DriveClient.

Every method funnels through ``_call``, which applies the retry policy and
translates HttpError into DriveApiError so nothing above this layer imports a
Google type. Query values are escaped before interpolation - a filename is
attacker-controllable and Drive's query language is injectable.
"""

from __future__ import annotations

import io
from collections.abc import Callable
from typing import Any, TypeVar

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload

from ..config import Settings
from ..errors import DriveApiError
from ..models import FOLDER_MIME, DriveFile
from .auth import load_credentials
from .retry import call_with_retry

T = TypeVar("T")

FIELDS = "id,name,mimeType,parents,size,md5Checksum,modifiedTime,trashed"
LIST_FIELDS = f"files({FIELDS}),nextPageToken"

#: Google-native types and the plain-text export they support.
EXPORTABLE_NATIVE = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
    "application/vnd.google-apps.presentation": "text/plain",
}
DOWNLOADABLE_TEXT = {"text/plain", "text/markdown", "text/csv"}


def escape_query_value(value: str) -> str:
    """Escape a literal for a Drive query string.

    Drive's ``q`` syntax is a small injectable language and file names come from
    the user's Drive, so this is untrusted input being interpolated.
    """
    return value.replace("\\", "\\\\").replace("'", "\\'")


def to_drive_file(payload: dict[str, Any]) -> DriveFile:
    size = payload.get("size")
    return DriveFile(
        id=payload["id"],
        name=payload.get("name", ""),
        mime_type=payload.get("mimeType", ""),
        parents=payload.get("parents", []) or [],
        size=int(size) if size is not None else None,
        md5_checksum=payload.get("md5Checksum"),
        modified_time=payload.get("modifiedTime"),
        trashed=bool(payload.get("trashed", False)),
    )


class GoogleDriveClient:
    def __init__(self, service: Any, sleep: Callable[[float], Any] | None = None) -> None:
        self._service = service
        self._sleep = sleep

    @classmethod
    def from_settings(cls, settings: Settings) -> GoogleDriveClient:
        credentials = load_credentials(settings, allow_interactive=False)
        service = build("drive", "v3", credentials=credentials, cache_discovery=False)
        return cls(service)

    # -- plumbing -----------------------------------------------------------

    def _call(self, description: str, operation: Callable[[], T]) -> T:
        def attempt() -> T:
            try:
                return operation()
            except HttpError as exc:
                status = getattr(exc.resp, "status", None)
                raise DriveApiError(
                    f"{description} failed: {exc.reason or 'Drive API error'}",
                    status=int(status) if status is not None else None,
                    operation=description,
                ) from exc

        kwargs: dict[str, Any] = {}
        if self._sleep is not None:
            kwargs["sleep"] = self._sleep
        return call_with_retry(attempt, **kwargs)

    @property
    def _files(self) -> Any:
        return self._service.files()

    # -- read ---------------------------------------------------------------

    def get_file(self, file_id: str) -> DriveFile:
        payload = self._call(
            f"get_file({file_id})",
            lambda: self._files.get(fileId=file_id, fields=FIELDS).execute(),
        )
        return to_drive_file(payload)

    def list_children(self, folder_id: str, page_size: int = 100) -> list[DriveFile]:
        query = f"'{escape_query_value(folder_id)}' in parents and trashed = false"
        return self._paged(query, page_size, f"list_children({folder_id})")

    def search(
        self, query: str, folder_id: str | None = None, page_size: int = 25
    ) -> list[DriveFile]:
        clauses = ["trashed = false"]
        if query.strip():
            clauses.append(f"name contains '{escape_query_value(query.strip())}'")
        if folder_id:
            clauses.append(f"'{escape_query_value(folder_id)}' in parents")
        return self._paged(" and ".join(clauses), page_size, "search")

    def _paged(self, query: str, page_size: int, description: str) -> list[DriveFile]:
        collected: list[DriveFile] = []
        page_token: str | None = None
        while len(collected) < page_size:
            token = page_token

            def fetch(token: str | None = token) -> dict[str, Any]:
                result: dict[str, Any] = self._files.list(
                    q=query,
                    fields=LIST_FIELDS,
                    pageSize=min(page_size - len(collected), 100),
                    pageToken=token,
                    spaces="drive",
                ).execute()
                return result

            response = self._call(description, fetch)
            collected.extend(to_drive_file(item) for item in response.get("files", []))
            page_token = response.get("nextPageToken")
            if not page_token:
                break
        return collected[:page_size]

    def export_text(self, file_id: str, max_chars: int) -> tuple[str, bool]:
        file = self.get_file(file_id)
        if file.mime_type in EXPORTABLE_NATIVE:
            request = self._files.export_media(
                fileId=file_id, mimeType=EXPORTABLE_NATIVE[file.mime_type]
            )
        elif file.mime_type in DOWNLOADABLE_TEXT:
            request = self._files.get_media(fileId=file_id)
        else:
            return "", False

        def download() -> bytes:
            buffer = io.BytesIO()
            downloader = MediaIoBaseDownload(buffer, request, chunksize=256 * 1024)
            done = False
            while not done:
                _, done = downloader.next_chunk()
                if buffer.tell() > max_chars * 4:  # generous UTF-8 headroom
                    break
            return buffer.getvalue()

        raw = self._call(f"export_text({file_id})", download)
        text = raw.decode("utf-8", errors="replace")
        if len(text) > max_chars:
            return text[:max_chars], True
        return text, False

    # -- write --------------------------------------------------------------

    def create_folder(self, name: str, parent_id: str) -> DriveFile:
        body = {"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]}
        payload = self._call(
            f"create_folder({name})",
            lambda: self._files.create(body=body, fields=FIELDS).execute(),
        )
        return to_drive_file(payload)

    def create_text_file(self, name: str, parent_id: str, content: str) -> DriveFile:
        """Provisioning only - used by `cwops workspace seed`, never by a tool.

        Not part of the DriveClient protocol on purpose: the MCP surface has no
        file-creation capability at all.
        """
        from googleapiclient.http import MediaInMemoryUpload

        body = {"name": name, "parents": [parent_id], "mimeType": "text/plain"}
        media = MediaInMemoryUpload(content.encode("utf-8"), mimetype="text/plain")
        payload = self._call(
            f"create_text_file({name})",
            lambda: self._files.create(body=body, media_body=media, fields=FIELDS).execute(),
        )
        return to_drive_file(payload)

    def rename(self, file_id: str, new_name: str) -> DriveFile:
        payload = self._call(
            f"rename({file_id})",
            lambda: self._files.update(
                fileId=file_id, body={"name": new_name}, fields=FIELDS
            ).execute(),
        )
        return to_drive_file(payload)

    def move(
        self, file_id: str, new_parent_id: str, expected_old_parent_id: str | None = None
    ) -> DriveFile:
        current = self.get_file(file_id)
        if expected_old_parent_id is not None and expected_old_parent_id not in current.parents:
            raise DriveApiError(
                f"{file_id} is no longer in {expected_old_parent_id}; it moved since "
                "planning.",
                status=409,
                file_id=file_id,
            )
        remove = ",".join(current.parents)
        payload = self._call(
            f"move({file_id})",
            lambda: self._files.update(
                fileId=file_id,
                addParents=new_parent_id,
                removeParents=remove,
                fields=FIELDS,
            ).execute(),
        )
        return to_drive_file(payload)
