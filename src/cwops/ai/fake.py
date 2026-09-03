"""Offline Organizer.

Two roles:

* demo mode - deterministic keyword rules stand in for the model so the whole
  pipeline runs with no Anthropic account;
* adversarial testing - ``FakeOrganizer.hostile()`` returns plans containing
  hallucinated IDs and escape attempts, proving the validator, not the prompt,
  is what keeps the system safe.
"""

from __future__ import annotations

from ..models import Classification, DraftAssignment, DraftFolder, DraftPlan, DriveFile

# Keyword -> (folder ref, folder name). Order matters: first match wins.
_RULES: tuple[tuple[tuple[str, ...], str, str], ...] = (
    (("invoice", "receipt"), "#invoices", "Invoices"),
    (("contract", "agreement", "vendor"), "#contracts", "Contracts"),
    (("tax",), "#tax", "Tax"),
    (("notes", "meeting", "sync"), "#notes", "Meeting Notes"),
    (("budget", "forecast", "model"), "#finance", "Finance"),
    (("photo", "image", "png", "jpg"), "#media", "Media"),
    (("spec", "product", "design"), "#specs", "Specifications"),
)
_FALLBACK = ("#misc", "Misc")


def _bucket(name: str) -> tuple[str, str]:
    lowered = name.lower()
    for keywords, ref, folder in _RULES:
        if any(keyword in lowered for keyword in keywords):
            return ref, folder
    return _FALLBACK


class FakeOrganizer:
    """Deterministic stand-in. Same interface, no network."""

    def __init__(self, plan_override: DraftPlan | None = None) -> None:
        self._plan_override = plan_override
        self.classify_calls = 0
        self.taxonomy_calls = 0

    @classmethod
    def hostile(cls, plan: DraftPlan) -> FakeOrganizer:
        """An Organizer that returns whatever hostile plan a test supplies."""
        return cls(plan_override=plan)

    def classify(
        self, files: list[DriveFile], snippets: dict[str, str]
    ) -> list[Classification]:
        self.classify_calls += 1
        results: list[Classification] = []
        for file in files:
            _, category = _bucket(file.name)
            has_text = bool(snippets.get(file.id))
            results.append(
                Classification(
                    file_id=file.id,
                    category=category,
                    topics=[category.lower()],
                    confidence=0.9 if category != _FALLBACK[1] else 0.4,
                    rationale=(
                        f"Keyword match on filename{' and content' if has_text else ''}."
                    ),
                )
            )
        return results

    def suggest_taxonomy(
        self,
        files: list[DriveFile],
        classifications: list[Classification],
        instructions: str | None,
    ) -> DraftPlan:
        self.taxonomy_calls += 1
        if self._plan_override is not None:
            return self._plan_override

        folders: dict[str, DraftFolder] = {}
        assignments: list[DraftAssignment] = []
        for file in files:
            if file.is_folder:
                continue
            ref, name = _bucket(file.name)
            folders.setdefault(ref, DraftFolder(ref=ref, name=name))
            assignments.append(DraftAssignment(file_id=file.id, folder_ref=ref))
        return DraftPlan(
            folders=sorted(folders.values(), key=lambda f: f.ref),
            assignments=assignments,
            rationale="Deterministic keyword grouping (offline organizer).",
        )
