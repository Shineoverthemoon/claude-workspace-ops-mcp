"""Retry, error mapping, and query escaping for the real Drive client.

No network: a stub service object stands in for googleapiclient, and sleep is
injected so real backoff logic runs instantly.
"""

from __future__ import annotations

from typing import Any

import pytest
from googleapiclient.errors import HttpError

from cwops.drive.client import GoogleDriveClient, escape_query_value, to_drive_file
from cwops.drive.retry import MAX_DELAY, RETRYABLE_STATUS, call_with_retry, is_retryable
from cwops.errors import DriveApiError


class FakeResponse:
    def __init__(self, status: int) -> None:
        self.status = status
        self.reason = f"HTTP {status}"


def http_error(status: int) -> HttpError:
    return HttpError(FakeResponse(status), b'{"error": {"message": "boom"}}')


# --- retry predicate ------------------------------------------------------


@pytest.mark.parametrize("status", sorted(RETRYABLE_STATUS))
def test_transient_statuses_are_retryable(status: int) -> None:
    assert is_retryable(DriveApiError("x", status=status))


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 412])
def test_client_errors_are_not_retryable(status: int) -> None:
    """404 under drive.file means 'not granted' - retrying only hides that."""
    assert not is_retryable(DriveApiError("x", status=status))


def test_network_errors_are_retryable() -> None:
    assert is_retryable(ConnectionError("reset"))
    assert is_retryable(TimeoutError("slow"))
    assert not is_retryable(ValueError("nope"))


# --- backoff behaviour ----------------------------------------------------


def test_retries_then_succeeds(sleeps: list[float]) -> None:
    calls = {"n": 0}

    def flaky() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise DriveApiError("rate limited", status=429)
        return "ok"

    assert call_with_retry(flaky, sleep=sleeps.append) == "ok"
    assert calls["n"] == 3
    assert len(sleeps) == 2


def test_gives_up_after_the_attempt_limit(sleeps: list[float]) -> None:
    calls = {"n": 0}

    def always_500() -> str:
        calls["n"] += 1
        raise DriveApiError("server error", status=500)

    with pytest.raises(DriveApiError):
        call_with_retry(always_500, attempts=4, sleep=sleeps.append)
    assert calls["n"] == 4
    assert len(sleeps) == 3


def test_non_retryable_errors_fail_immediately(sleeps: list[float]) -> None:
    calls = {"n": 0}

    def not_found() -> str:
        calls["n"] += 1
        raise DriveApiError("missing", status=404)

    with pytest.raises(DriveApiError):
        call_with_retry(not_found, sleep=sleeps.append)
    assert calls["n"] == 1
    assert sleeps == []


def test_backoff_is_bounded_and_increasing(sleeps: list[float]) -> None:
    def always_503() -> str:
        raise DriveApiError("unavailable", status=503)

    with pytest.raises(DriveApiError):
        call_with_retry(always_503, attempts=6, sleep=sleeps.append)
    assert all(0 <= delay <= MAX_DELAY for delay in sleeps)
    assert sleeps[-1] >= sleeps[0]


def test_success_on_first_try_never_sleeps(sleeps: list[float]) -> None:
    assert call_with_retry(lambda: 42, sleep=sleeps.append) == 42
    assert sleeps == []


# --- HttpError translation ------------------------------------------------


class StubFiles:
    def __init__(self, script: list[Any]) -> None:
        self.script = script
        self.calls: list[dict[str, Any]] = []

    def _next(self) -> Any:
        outcome = self.script.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def get(self, **kwargs: Any) -> Any:
        self.calls.append({"method": "get", **kwargs})
        return self

    def list(self, **kwargs: Any) -> Any:
        self.calls.append({"method": "list", **kwargs})
        return self

    def update(self, **kwargs: Any) -> Any:
        self.calls.append({"method": "update", **kwargs})
        return self

    def create(self, **kwargs: Any) -> Any:
        self.calls.append({"method": "create", **kwargs})
        return self

    def execute(self) -> Any:
        return self._next()


class StubService:
    def __init__(self, files: StubFiles) -> None:
        self._files = files

    def files(self) -> StubFiles:
        return self._files


def client_for(script: list[Any], sleeps: list[float]) -> tuple[GoogleDriveClient, StubFiles]:
    files = StubFiles(script)
    return GoogleDriveClient(StubService(files), sleep=sleeps.append), files


FILE_PAYLOAD = {
    "id": "f1",
    "name": "Invoice.pdf",
    "mimeType": "application/pdf",
    "parents": ["fol"],
    "size": "1234",
    "md5Checksum": "abc",
    "modifiedTime": "2026-01-01T00:00:00.000Z",
}


def test_http_error_becomes_a_domain_error(sleeps: list[float]) -> None:
    client, _ = client_for([http_error(404)], sleeps)
    with pytest.raises(DriveApiError) as exc:
        client.get_file("f1")
    assert exc.value.context["status"] == 404
    assert exc.value.reason_code == "drive_api_error"


def test_a_transient_http_error_is_retried_then_succeeds(sleeps: list[float]) -> None:
    client, _ = client_for([http_error(503), FILE_PAYLOAD], sleeps)
    assert client.get_file("f1").name == "Invoice.pdf"
    assert len(sleeps) == 1


def test_no_google_exception_escapes_the_drive_layer(sleeps: list[float]) -> None:
    client, _ = client_for([http_error(500)] * 4, sleeps)
    with pytest.raises(DriveApiError):
        client.get_file("f1")


# --- payload mapping ------------------------------------------------------


def test_payload_mapping_normalizes_types() -> None:
    file = to_drive_file(FILE_PAYLOAD)
    assert file.size == 1234  # Drive returns size as a string
    assert file.md5_checksum == "abc"
    assert file.trashed is False
    assert file.modified_time is not None


def test_missing_optional_fields_are_tolerated() -> None:
    file = to_drive_file({"id": "x", "name": "Doc", "mimeType": "application/pdf"})
    assert file.size is None and file.parents == []


# --- query escaping -------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("plain", "plain"),
        ("it's", "it\\'s"),
        ("back\\slash", "back\\\\slash"),
        ("' or name contains '", "\\' or name contains \\'"),
    ],
)
def test_query_values_are_escaped(raw: str, expected: str) -> None:
    assert escape_query_value(raw) == expected


def test_search_escapes_the_user_supplied_query(sleeps: list[float]) -> None:
    """A filename is untrusted input being interpolated into Drive's query DSL."""
    client, files = client_for([{"files": []}], sleeps)
    client.search("' or trashed = true or name contains '")
    query = files.calls[-1]["q"]
    assert "\\'" in query
    assert query.count("'") == query.count("\\'") + 2  # only our own delimiters


def test_list_children_scopes_to_the_folder_and_excludes_trash(
    sleeps: list[float],
) -> None:
    client, files = client_for([{"files": [FILE_PAYLOAD]}], sleeps)
    client.list_children("fol-inbox")
    query = files.calls[-1]["q"]
    assert "'fol-inbox' in parents" in query
    assert "trashed = false" in query


def test_move_refuses_when_the_file_already_moved(sleeps: list[float]) -> None:
    client, _ = client_for([FILE_PAYLOAD], sleeps)
    with pytest.raises(DriveApiError) as exc:
        client.move("f1", "fol-new", expected_old_parent_id="somewhere-else")
    assert exc.value.context["status"] == 409


def test_move_removes_the_old_parent(sleeps: list[float]) -> None:
    client, files = client_for([FILE_PAYLOAD, {**FILE_PAYLOAD, "parents": ["fol-new"]}], sleeps)
    client.move("f1", "fol-new", expected_old_parent_id="fol")
    update = files.calls[-1]
    assert update["addParents"] == "fol-new"
    assert update["removeParents"] == "fol"
