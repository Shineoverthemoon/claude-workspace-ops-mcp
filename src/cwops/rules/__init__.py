"""Deterministic rules. Pure functions, no I/O, no network, no model.

Everything in this package is total and side-effect free, which is why it can be
tested exhaustively and why it is the layer that authorizes actions.
"""

from .duplicates import find_duplicates
from .naming import NameProblem, check_name, normalize_title, split_extension
from .validate import ValidationContext, render_preview, validate_plan

__all__ = [
    "NameProblem",
    "ValidationContext",
    "check_name",
    "find_duplicates",
    "normalize_title",
    "render_preview",
    "split_extension",
    "validate_plan",
]
