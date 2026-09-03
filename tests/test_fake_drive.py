"""The fake must behave like the drive.file scope, or every later test lies."""

from __future__ import annotations

import pytest

from cwops.drive import DriveClient, FakeDriveClient
from cwops.errors import DriveApiError


def test_fake_satisfies_the_protocol(drive: FakeDriveClient) -> None:
    assert isinstance(drive, DriveClient)


def test_ungranted_file_is_404_not_403(drive: FakeDriveClient) -> None:
    """drive.file hides existence; it does not report a permission denial."""
    with pytest.raises(DriveApiError) as exc:
        drive.get_file("f-not-granted")
    assert exc.value.context["status"] == 404


def test_ungranted_file_is_invisible_to_listing_and_search(drive: FakeDriveClient) -> None:
    child_ids = {f.id for f in drive.list_children("fol-inbox")}
    assert "f-not-granted" not in child_ids
    assert drive.search("Confidential") == []


def test_granted_file_outside_the_workspace_is_reachable(drive: FakeDriveClient) -> None:
    """A Picker-shared file is legitimately readable but lives outside the root."""
    file = drive.get_file("f-outside-workspace")
    assert file.parents == ["other-root"]


def test_trashed_files_are_excluded_from_listings(drive: FakeDriveClient) -> None:
    assert "f-trashed" not in {f.id for f in drive.list_children("fol-inbox")}


def test_listing_puts_folders_first(drive: FakeDriveClient) -> None:
    children = drive.list_children("root-workspace")
    assert [c.name for c in children[:2]] == ["Archive", "Inbox"]


def test_export_text_truncates(drive: FakeDriveClient) -> None:
    text, truncated = drive.export_text("f-notes", max_chars=20)
    assert truncated is True
    assert len(text) == 20


def test_export_text_returns_empty_for_binary(drive: FakeDriveClient) -> None:
    text, truncated = drive.export_text("f-photo", max_chars=1000)
    assert (text, truncated) == ("", False)


def test_created_folder_is_auto_granted(drive: FakeDriveClient) -> None:
    folder = drive.create_folder("Invoices", "root-workspace")
    assert drive.get_file(folder.id).name == "Invoices"


def test_move_detects_concurrent_relocation(drive: FakeDriveClient) -> None:
    with pytest.raises(DriveApiError) as exc:
        drive.move("f-inv-q1", "fol-archive", expected_old_parent_id="fol-archive")
    assert exc.value.context["status"] == 409


def test_call_recording_separates_reads_from_mutations(drive: FakeDriveClient) -> None:
    drive.search("invoice")
    drive.list_children("fol-inbox")
    assert drive.mutating_calls == []
    drive.rename("f-inv-q1", "Invoice Q1 2026 FINAL.pdf")
    assert [c.method for c in drive.mutating_calls] == ["rename"]


def test_queued_errors_fire_once(drive: FakeDriveClient) -> None:
    drive.queue_error("get_file", DriveApiError("boom", status=503), file_id="f-inv-q1")
    with pytest.raises(DriveApiError):
        drive.get_file("f-inv-q1")
    assert drive.get_file("f-inv-q1").id == "f-inv-q1"


def test_seed_shape_supports_the_duplicate_scenarios(drive: FakeDriveClient) -> None:
    inbox = drive.list_children("fol-inbox")
    exact = [f for f in inbox if f.md5_checksum == "a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1"]
    assert len(exact) == 3
    native = [f for f in inbox if f.is_google_native and f.md5_checksum is None]
    assert len(native) >= 3
