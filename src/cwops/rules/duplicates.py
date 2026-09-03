"""Deterministic duplicate detection. No model involved, no file bytes read.

Two strategies with honestly different confidence:

* ``checksum`` - exact match on Drive's md5Checksum plus size. Trustworthy, but
  Google-native Docs/Sheets/Slides have no checksum at all, so those files are
  reported as *unscannable* rather than quietly treated as unique.
* ``metadata`` - normalized title plus size and MIME type. Catches the copies
  the checksum strategy structurally cannot see, and is always labelled
  ``heuristic`` so a caller never mistakes it for proof.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Literal

from ..models import DriveFile, DuplicateGroup, DuplicateReport, UnscannableFile
from .naming import normalize_title

Strategy = Literal["checksum", "metadata"]

NO_CHECKSUM_NATIVE = "Google-native format; Drive exposes no checksum for it"
NO_CHECKSUM_OTHER = "No checksum reported by Drive for this file"


def find_duplicates(files: list[DriveFile], strategy: Strategy = "checksum") -> DuplicateReport:
    """Group ``files`` into duplicate sets. Folders and trashed files are ignored."""
    candidates = [f for f in files if not f.is_folder and not f.trashed]
    if strategy == "checksum":
        groups, unscannable = _by_checksum(candidates)
    else:
        groups, unscannable = _by_metadata(candidates)
    return DuplicateReport(
        strategy=strategy,
        scanned=len(candidates),
        groups=groups,
        unscannable=unscannable,
    )


def _by_checksum(
    files: list[DriveFile],
) -> tuple[list[DuplicateGroup], list[UnscannableFile]]:
    buckets: dict[str, list[DriveFile]] = defaultdict(list)
    unscannable: list[UnscannableFile] = []
    for file in files:
        if not file.md5_checksum:
            unscannable.append(
                UnscannableFile(
                    file_id=file.id,
                    name=file.name,
                    mime_type=file.mime_type,
                    reason=NO_CHECKSUM_NATIVE if file.is_google_native else NO_CHECKSUM_OTHER,
                )
            )
            continue
        buckets[f"{file.md5_checksum}:{file.size if file.size is not None else '-'}"].append(file)
    groups = [
        DuplicateGroup(reason="checksum", confidence="exact", key=key, files=_sorted(members))
        for key, members in sorted(buckets.items())
        if len(members) > 1
    ]
    # Sorted so the whole report is a pure function of the input *set*, not of
    # the order Drive happened to page results in.
    unscannable.sort(key=lambda u: u.file_id)
    return groups, unscannable


def _by_metadata(
    files: list[DriveFile],
) -> tuple[list[DuplicateGroup], list[UnscannableFile]]:
    buckets: dict[str, list[DriveFile]] = defaultdict(list)
    for file in files:
        # Size is part of the key only when Drive reports one; Google-native
        # docs have no size, so those group on title + type alone.
        size_part = str(file.size) if file.size is not None else "native"
        buckets[f"{normalize_title(file.name)}|{file.mime_type}|{size_part}"].append(file)
    groups = [
        DuplicateGroup(reason="metadata", confidence="heuristic", key=key, files=_sorted(members))
        for key, members in sorted(buckets.items())
        if len(members) > 1
    ]
    return groups, []


def _sorted(members: list[DriveFile]) -> list[DriveFile]:
    """Oldest first, so the first entry is the natural 'keep' candidate.

    Sorted on the ISO string rather than the datetime so a missing timestamp
    sorts first instead of raising on a None-vs-datetime comparison.
    """
    return sorted(
        members,
        key=lambda f: (f.modified_time.isoformat() if f.modified_time else "", f.id),
    )
