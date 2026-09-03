"""The Claude-backed Organizer.

Two calls, both narrow, both schema-constrained via structured outputs so the
response is validated against a Pydantic model before this module returns. Even
then the result is only advisory - cwops.rules re-checks everything.

Model choice, effort and thinking follow the current API: adaptive thinking with
a low effort level, since classification is a cheap, well-specified task rather
than a reasoning-heavy one.
"""

from __future__ import annotations

import logging
from typing import Any

import anthropic
from anthropic.types import OutputConfigParam
from pydantic import BaseModel, ValidationError

from ..config import Settings
from ..errors import AiResponseError
from ..logging import get_logger, log_event
from ..models import Classification, DraftPlan, DriveFile
from .prompts import CLASSIFY_SYSTEM, TAXONOMY_SYSTEM, render_file_list

logger = get_logger("ai.claude")

MAX_TOKENS = 8000
MAX_EXCERPT_CHARS = 1200


class ClassificationBatch(BaseModel):
    """Wrapper so structured output has a single top-level object."""

    classifications: list[Classification]


class ClaudeOrganizer:
    def __init__(
        self,
        client: anthropic.Anthropic,
        model: str = "claude-opus-5",
        effort: str = "low",
    ) -> None:
        self._client = client
        self._model = model
        self._effort = effort
        # Classification is a cheap, well-specified task: low effort with
        # adaptive thinking is the right cost/quality point, not a downgrade.
        self._output_config = OutputConfigParam(effort=effort)  # type: ignore[typeddict-item]

    @classmethod
    def from_settings(cls, settings: Settings) -> ClaudeOrganizer:
        settings.require_anthropic_key()
        # The SDK already retries 408/409/429/5xx with exponential backoff.
        client = anthropic.Anthropic(max_retries=4, timeout=60.0)
        return cls(client, model=settings.claude_model, effort=settings.claude_effort)

    # -- internals ----------------------------------------------------------

    def _entries(
        self, files: list[DriveFile], snippets: dict[str, str]
    ) -> list[dict[str, Any]]:
        return [
            {
                "id": file.id,
                "name": file.name,
                "mime_type": file.mime_type,
                "size": file.size,
                "excerpt": (snippets.get(file.id) or "")[:MAX_EXCERPT_CHARS] or None,
            }
            for file in files
            if not file.is_folder
        ]

    def _parse(self, system: str, user: str, schema: type[BaseModel]) -> Any:
        try:
            response = self._client.messages.parse(
                model=self._model,
                max_tokens=MAX_TOKENS,
                # Stable prefix first, volatile content in the user turn, so the
                # cache actually hits across calls.
                system=[
                    {
                        "type": "text",
                        "text": system,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                thinking={"type": "adaptive"},
                output_config=self._output_config,
                messages=[{"role": "user", "content": user}],
                output_format=schema,
            )
        except anthropic.APIStatusError as exc:
            raise AiResponseError(
                f"Claude request failed with HTTP {exc.status_code}.",
                status=exc.status_code,
            ) from exc
        except anthropic.APIConnectionError as exc:
            raise AiResponseError("Could not reach the Anthropic API.") from exc

        usage = response.usage
        log_event(
            logger,
            logging.INFO,
            "claude.call",
            model=self._model,
            effort=self._effort,
            schema=schema.__name__,
            input_tokens=getattr(usage, "input_tokens", None),
            output_tokens=getattr(usage, "output_tokens", None),
            cache_read_input_tokens=getattr(usage, "cache_read_input_tokens", None),
            stop_reason=response.stop_reason,
            request_id=response._request_id,
        )

        if response.stop_reason == "refusal":
            raise AiResponseError(
                "Claude declined this request.",
                category=getattr(response.stop_details, "category", None),
            )
        parsed = response.parsed_output
        if parsed is None:
            raise AiResponseError("Claude returned no parseable structured output.")
        return parsed

    # -- Organizer protocol -------------------------------------------------

    def classify(
        self, files: list[DriveFile], snippets: dict[str, str]
    ) -> list[Classification]:
        entries = self._entries(files, snippets)
        if not entries:
            return []
        user = (
            "Classify these files.\n\n"
            f"{render_file_list(entries)}\n\n"
            "Return one classification per file."
        )
        try:
            batch: ClassificationBatch = self._parse(
                CLASSIFY_SYSTEM, user, ClassificationBatch
            )
        except ValidationError as exc:
            raise AiResponseError("Classification response failed schema validation.") from exc
        return batch.classifications

    def suggest_taxonomy(
        self,
        files: list[DriveFile],
        classifications: list[Classification],
        instructions: str | None,
    ) -> DraftPlan:
        entries = self._entries(files, {})
        if not entries:
            return DraftPlan(rationale="No files to organize.")
        labels = "\n".join(
            f"- {item.file_id}: {item.category} (confidence {item.confidence:.2f})"
            for item in classifications
        )
        user = (
            "Propose a folder structure for these files.\n\n"
            f"FILES:\n{render_file_list(entries)}\n\n"
            f"CLASSIFICATIONS:\n{labels or '(none)'}\n"
        )
        if instructions:
            # User instructions are quoted, not blended into the system prompt,
            # so they cannot silently redefine the task's rules.
            user += f"\nUSER PREFERENCE (advisory):\n{instructions[:1000]}\n"
        try:
            plan: DraftPlan = self._parse(TAXONOMY_SYSTEM, user, DraftPlan)
        except ValidationError as exc:
            raise AiResponseError("Taxonomy response failed schema validation.") from exc
        return plan
