"""The Organizer contract - where probabilistic reasoning is allowed in.

Both methods are advisory. Neither returns an authorization, an ID the server
did not already know, or an operation the server did not already permit: the
return types are too narrow to express one.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..models import Classification, DraftPlan, DriveFile


@runtime_checkable
class Organizer(Protocol):
    def classify(
        self, files: list[DriveFile], snippets: dict[str, str]
    ) -> list[Classification]:
        """Label ambiguous documents. Genuine judgment; no deterministic rule fits."""
        ...

    def suggest_taxonomy(
        self,
        files: list[DriveFile],
        classifications: list[Classification],
        instructions: str | None,
    ) -> DraftPlan:
        """Propose a folder structure and file assignments."""
        ...
