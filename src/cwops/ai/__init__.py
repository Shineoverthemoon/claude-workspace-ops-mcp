"""The only place a language model is consulted.

Two advisory functions, both returning schema-constrained data that is then
re-validated by cwops.rules before it can influence any action.
"""

from .fake import FakeOrganizer
from .protocol import Organizer

__all__ = ["FakeOrganizer", "Organizer"]
