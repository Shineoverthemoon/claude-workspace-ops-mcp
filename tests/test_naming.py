from __future__ import annotations

import pytest

from cwops.rules.naming import (
    MAX_NAME_LENGTH,
    NameProblem,
    check_name,
    is_safe_name,
    normalize_title,
    split_extension,
    strip_copy_markers,
)


@pytest.mark.parametrize(
    "name",
    [
        "Invoices",
        "Invoice Q1 2026.pdf",
        "FY26 Budget Model",
        ".hidden-but-legal",
        "Ünïcödé Ñame",
        "a" * MAX_NAME_LENGTH,
    ],
)
def test_accepts_ordinary_names(name: str) -> None:
    assert check_name(name) is NameProblem.OK
    assert is_safe_name(name)


@pytest.mark.parametrize(
    ("name", "problem"),
    [
        ("", NameProblem.EMPTY),
        ("   ", NameProblem.EMPTY),
        ("../escape", NameProblem.PATH_SEPARATOR),
        ("..\\escape", NameProblem.PATH_SEPARATOR),
        ("folder/sub", NameProblem.PATH_SEPARATOR),
        ("/absolute", NameProblem.PATH_SEPARATOR),
        ("..", NameProblem.RESERVED),
        (".", NameProblem.RESERVED),
        (" leading", NameProblem.LEADING_OR_TRAILING_SPACE),
        ("trailing ", NameProblem.LEADING_OR_TRAILING_SPACE),
        ("dot.", NameProblem.TRAILING_DOT),
        ("bell\x07here", NameProblem.CONTROL_CHARACTER),
        ("newline\nhere", NameProblem.CONTROL_CHARACTER),
        ("null\x00byte", NameProblem.CONTROL_CHARACTER),
        ("a" * (MAX_NAME_LENGTH + 1), NameProblem.TOO_LONG),
    ],
)
def test_rejects_unsafe_names(name: str, problem: NameProblem) -> None:
    assert check_name(name) is problem
    assert not is_safe_name(name)


def test_check_name_never_repairs() -> None:
    """It returns a verdict, not a cleaned string - callers cannot 'fix and go'."""
    assert isinstance(check_name("../evil"), NameProblem)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Invoice.pdf", ("Invoice", ".pdf")),
        ("archive.TAR", ("archive", ".tar")),
        ("Meeting Notes 2026-01-14", ("Meeting Notes 2026-01-14", "")),
        (".hidden", (".hidden", "")),
        ("name.with.many.dots.txt", ("name.with.many.dots", ".txt")),
        ("weird.extensionistoolong", ("weird.extensionistoolong", "")),
    ],
)
def test_split_extension(name: str, expected: tuple[str, str]) -> None:
    assert split_extension(name) == expected


@pytest.mark.parametrize(
    ("stem", "expected"),
    [
        ("Report (1)", "Report"),
        ("Report copy", "Report"),
        ("Report Copy", "Report"),
        ("Copy of Report", "Report"),
        ("Copy of Report copy (2)", "Report"),
        ("Report - Copy", "Report"),
        ("Report", "Report"),
    ],
)
def test_strip_copy_markers(stem: str, expected: str) -> None:
    assert strip_copy_markers(stem) == expected


def test_normalize_title_collapses_the_seed_duplicates() -> None:
    canonical = normalize_title("Invoice Q1 2026.pdf")
    assert normalize_title("Copy of Invoice Q1 2026.pdf") == canonical
    assert normalize_title("Invoice Q1 2026 (1).pdf") == canonical
    assert normalize_title("INVOICE  Q1   2026.pdf") == canonical


def test_normalize_title_keeps_genuinely_different_names_apart() -> None:
    assert normalize_title("team-photo.png") != normalize_title("team-photo-final.png")
    assert normalize_title("Invoice Q1 2026.pdf") != normalize_title("Invoice Q2 2026.pdf")


def test_normalize_title_folds_diacritics_and_separators() -> None:
    assert normalize_title("Résumé_Final.docx") == normalize_title("Resume Final.docx")


def test_normalize_title_keeps_extension_significant() -> None:
    assert normalize_title("Report.pdf") != normalize_title("Report.docx")
