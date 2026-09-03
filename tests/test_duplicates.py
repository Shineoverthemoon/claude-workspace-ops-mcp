from __future__ import annotations

from datetime import UTC, datetime

from cwops.models import FOLDER_MIME, DriveFile
from cwops.rules import find_duplicates


def file(
    file_id: str,
    name: str,
    *,
    mime: str = "application/pdf",
    size: int | None = 100,
    md5: str | None = "aaa",
    trashed: bool = False,
    minute: int = 0,
) -> DriveFile:
    return DriveFile(
        id=file_id,
        name=name,
        mime_type=mime,
        parents=["fol-inbox"],
        size=size,
        md5_checksum=md5,
        trashed=trashed,
        modified_time=datetime(2026, 1, 1, 12, minute, tzinfo=UTC),
    )


def all_seed_files(known_files: dict[str, DriveFile], granted_ids: set[str]) -> list[DriveFile]:
    return [f for fid, f in known_files.items() if fid in granted_ids]


# --- checksum strategy ----------------------------------------------------


def test_checksum_groups_exact_matches(
    known_files: dict[str, DriveFile], granted_ids: set[str]
) -> None:
    report = find_duplicates(all_seed_files(known_files, granted_ids), "checksum")
    grouped = {frozenset(f.id for f in group.files) for group in report.groups}
    assert frozenset({"f-inv-q1", "f-inv-q1-copy", "f-inv-q1-1"}) in grouped
    assert frozenset({"f-contract", "f-contract-dup"}) in grouped
    assert frozenset({"f-spec", "f-spec-archived"}) in grouped
    assert all(group.confidence == "exact" for group in report.groups)


def test_checksum_does_not_group_same_size_different_content(
    known_files: dict[str, DriveFile], granted_ids: set[str]
) -> None:
    report = find_duplicates(all_seed_files(known_files, granted_ids), "checksum")
    for group in report.groups:
        ids = {f.id for f in group.files}
        assert not {"f-photo", "f-photo-final"} <= ids


def test_google_native_files_are_unscannable_not_unique(
    known_files: dict[str, DriveFile], granted_ids: set[str]
) -> None:
    """The honest part: no checksum means 'cannot tell', not 'not a duplicate'."""
    report = find_duplicates(all_seed_files(known_files, granted_ids), "checksum")
    unscannable = {u.file_id for u in report.unscannable}
    assert {"f-notes", "f-notes-dup", "f-budget"} <= unscannable
    reason = next(u.reason for u in report.unscannable if u.file_id == "f-notes")
    assert "no checksum" in reason.lower()


def test_folders_and_trashed_files_are_never_candidates() -> None:
    files = [
        file("fol", "Folder", mime=FOLDER_MIME, size=None, md5=None),
        file("a", "A.pdf"),
        file("b", "B.pdf", trashed=True),
    ]
    report = find_duplicates(files, "checksum")
    assert report.scanned == 1
    assert report.groups == []


def test_singletons_are_not_reported() -> None:
    report = find_duplicates([file("a", "A.pdf", md5="x"), file("b", "B.pdf", md5="y")], "checksum")
    assert report.groups == []


def test_empty_input_is_handled() -> None:
    report = find_duplicates([], "checksum")
    assert (report.scanned, report.groups, report.unscannable) == (0, [], [])


def test_group_members_are_oldest_first() -> None:
    files = [file("new", "A.pdf", minute=30), file("old", "A copy.pdf", minute=1)]
    group = find_duplicates(files, "checksum").groups[0]
    assert [f.id for f in group.files] == ["old", "new"]


# --- metadata strategy ----------------------------------------------------


def test_metadata_catches_google_native_copies(
    known_files: dict[str, DriveFile], granted_ids: set[str]
) -> None:
    """The whole point of the second strategy: checksum structurally cannot."""
    report = find_duplicates(all_seed_files(known_files, granted_ids), "metadata")
    grouped = {frozenset(f.id for f in group.files) for group in report.groups}
    assert frozenset({"f-notes", "f-notes-dup"}) in grouped


def test_metadata_results_are_labelled_heuristic(
    known_files: dict[str, DriveFile], granted_ids: set[str]
) -> None:
    report = find_duplicates(all_seed_files(known_files, granted_ids), "metadata")
    assert report.groups
    assert all(group.confidence == "heuristic" for group in report.groups)
    assert report.unscannable == []


def test_metadata_requires_matching_mime_type() -> None:
    files = [
        file("a", "Report.pdf", mime="application/pdf", md5=None),
        file("b", "Report.pdf", mime="text/plain", md5=None),
    ]
    assert find_duplicates(files, "metadata").groups == []


def test_metadata_requires_matching_size_when_known() -> None:
    files = [file("a", "Report.pdf", size=100), file("b", "Report (1).pdf", size=200)]
    assert find_duplicates(files, "metadata").groups == []


def test_metadata_does_not_group_unrelated_names(
    known_files: dict[str, DriveFile], granted_ids: set[str]
) -> None:
    report = find_duplicates(all_seed_files(known_files, granted_ids), "metadata")
    for group in report.groups:
        ids = {f.id for f in group.files}
        assert not {"f-inv-q1", "f-inv-q2"} <= ids


def test_output_is_deterministic(
    known_files: dict[str, DriveFile], granted_ids: set[str]
) -> None:
    files = all_seed_files(known_files, granted_ids)
    first = find_duplicates(files, "checksum")
    second = find_duplicates(list(reversed(files)), "checksum")
    assert first.model_dump() == second.model_dump()
