"""Filename policy and normalization.

Two separate jobs, deliberately kept apart:

* ``check_name`` is a *security* check applied to every name a plan wants to
  write. It is conservative and returns a reason, never a repaired string - a
  validator that silently fixes hostile input teaches callers to send it.
* ``normalize_title`` is a *comparison* helper for heuristic duplicate
  detection. It is lossy on purpose and never feeds back into a write.
"""

from __future__ import annotations

import re
import unicodedata
from enum import StrEnum

MAX_NAME_LENGTH = 120

_PATH_SEPARATORS = ("/", "\\")
_RESERVED_NAMES = {".", ".."}
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")

# Copy markers stripped before comparing two titles. Order matters: the numeric
# suffix is removed before the trailing "copy" so "Report copy (2)" collapses.
_COPY_PATTERNS = (
    re.compile(r"\s*\((\d+)\)\s*$"),
    re.compile(r"\s*[-–—]?\s*copy\s*$", re.IGNORECASE),
    re.compile(r"^\s*copy\s+of\s+", re.IGNORECASE),
    re.compile(r"\s*[-–—]\s*copy\s*\(\d+\)\s*$", re.IGNORECASE),
)

_KNOWN_EXTENSIONS_MAX_LEN = 8


class NameProblem(StrEnum):
    """Why a proposed name was refused. ``OK`` means the name is acceptable."""

    OK = "ok"
    EMPTY = "empty"
    PATH_SEPARATOR = "path_separator"
    RESERVED = "reserved"
    CONTROL_CHARACTER = "control_character"
    TOO_LONG = "too_long"
    LEADING_OR_TRAILING_SPACE = "leading_or_trailing_space"
    TRAILING_DOT = "trailing_dot"


def check_name(name: str) -> NameProblem:
    """Validate a name a plan wants to write to Drive.

    Rejects path separators and the reserved ``.``/``..`` names, which is what
    stops a model-suggested folder name from escaping the workspace.
    """
    if not name or not name.strip():
        return NameProblem.EMPTY
    if name != name.strip():
        return NameProblem.LEADING_OR_TRAILING_SPACE
    if any(sep in name for sep in _PATH_SEPARATORS):
        return NameProblem.PATH_SEPARATOR
    if name in _RESERVED_NAMES:
        return NameProblem.RESERVED
    if _CONTROL_CHARS.search(name):
        return NameProblem.CONTROL_CHARACTER
    if name.endswith("."):
        return NameProblem.TRAILING_DOT
    if len(name) > MAX_NAME_LENGTH:
        return NameProblem.TOO_LONG
    return NameProblem.OK


def is_safe_name(name: str) -> bool:
    return check_name(name) is NameProblem.OK


def split_extension(name: str) -> tuple[str, str]:
    """Split ``('Report', '.pdf')``. Returns an empty extension when unclear."""
    if "." not in name[1:]:
        return name, ""
    stem, _, ext = name.rpartition(".")
    if not ext or len(ext) > _KNOWN_EXTENSIONS_MAX_LEN or not ext.isalnum():
        return name, ""
    return stem, f".{ext.lower()}"


def strip_copy_markers(stem: str) -> str:
    """Remove ``(1)``, ``copy``, ``Copy of`` and friends, repeatedly."""
    current = stem
    for _ in range(4):  # bounded: handles "Copy of Report copy (2)"
        previous = current
        for pattern in _COPY_PATTERNS:
            current = pattern.sub("", current)
        current = current.strip()
        if current == previous:
            break
    return current


def normalize_title(name: str) -> str:
    """Lossy canonical form used only for heuristic duplicate grouping."""
    stem, ext = split_extension(name)
    stem = strip_copy_markers(stem)
    stem = unicodedata.normalize("NFKD", stem)
    stem = "".join(ch for ch in stem if not unicodedata.combining(ch))
    stem = re.sub(r"[\s_]+", " ", stem).strip().casefold()
    return f"{stem}{ext}"
