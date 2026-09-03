"""Environment-variable configuration. Fails fast on unsafe combinations.

No secret value is ever stored here beyond a filesystem path; the Anthropic SDK
reads ANTHROPIC_API_KEY from the environment itself and Google credentials live
in a 0600 token file.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .errors import ConfigurationError

# Least privilege: this is the ONLY scope the app ever requests.
#
# drive.file is Google's one non-sensitive Drive scope. It grants per-file
# access to files this app created *and* files the user explicitly opened or
# shared with it (Google Picker / "Open with"). It cannot enumerate or touch
# anything else in the user's Drive, so a bug or a prompt injection has no
# reachable blast radius beyond the granted set.
DRIVE_FILE_SCOPE = "https://www.googleapis.com/auth/drive.file"
SCOPES: tuple[str, ...] = (DRIVE_FILE_SCOPE,)

WORKSPACE_FOLDER_NAME = "Claude Workspace Ops"


class Settings(BaseSettings):
    """All runtime configuration. Every field is settable via CWOPS_* env vars."""

    model_config = SettingsConfigDict(
        env_prefix="CWOPS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- backends ---
    drive_backend: Literal["fake", "google"] = "fake"
    ai_backend: Literal["fake", "claude"] = "fake"

    # --- safety ---
    allow_mutations: bool = False
    root_folder_id: str = ""

    # --- google ---
    google_client_secrets: Path = Path("client_secret.json")
    token_path: Path = Path(".cwops/token.json")

    # --- claude ---
    claude_model: str = "claude-opus-5"
    claude_effort: Literal["low", "medium", "high"] = "low"

    # --- limits / storage ---
    db_path: Path = Path("cwops.db")
    max_files_per_proposal: int = Field(default=50, ge=1, le=200)
    max_file_chars: int = Field(default=20_000, ge=200, le=200_000)
    approval_ttl_seconds: int = Field(default=900, ge=30, le=86_400)
    log_level: str = "INFO"

    @model_validator(mode="after")
    def _check_safety_invariants(self) -> Settings:
        # Fail fast: mutations without a bounded workspace would mean an
        # unbounded blast radius. Refuse to start rather than infer a default.
        if self.allow_mutations and not self.root_folder_id.strip():
            raise ValueError(
                "CWOPS_ALLOW_MUTATIONS=true requires CWOPS_ROOT_FOLDER_ID. "
                "Run `cwops workspace init` to create the app-owned workspace."
            )
        return self

    @property
    def scopes(self) -> list[str]:
        return list(SCOPES)

    def require_google_credentials(self) -> Path:
        """Return the client-secrets path, or raise with an actionable message."""
        path = self.google_client_secrets.expanduser()
        if not path.is_file():
            raise ConfigurationError(
                f"Google client secrets file not found: {path}",
                env_var="CWOPS_GOOGLE_CLIENT_SECRETS",
            )
        return path

    def require_root_folder(self) -> str:
        root = self.root_folder_id.strip()
        if not root:
            raise ConfigurationError(
                "CWOPS_ROOT_FOLDER_ID is not set. Run `cwops workspace init` first.",
                env_var="CWOPS_ROOT_FOLDER_ID",
            )
        return root

    def require_anthropic_key(self) -> None:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise ConfigurationError(
                "ANTHROPIC_API_KEY is not set but CWOPS_AI_BACKEND=claude.",
                env_var="ANTHROPIC_API_KEY",
            )


def load_settings(**overrides: object) -> Settings:
    """Build Settings, translating pydantic validation failures into our error type."""
    try:
        return Settings(**overrides)  # type: ignore[arg-type]
    except ValueError as exc:
        raise ConfigurationError(str(exc)) from exc
