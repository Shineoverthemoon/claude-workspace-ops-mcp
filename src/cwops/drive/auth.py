"""Google OAuth, restricted to drive.file.

Least privilege is enforced twice here: the flow requests only drive.file, and
``_assert_least_privilege`` refuses to use a token that came back carrying any
broader scope. A stored token that somehow granted full Drive access is treated
as a configuration error, not as a bonus.

drive.file is Google's only non-sensitive Drive scope, so this app does not
trigger the sensitive/restricted-scope verification review. That is independent
of publishing status: while the OAuth app remains in Testing, Google issues
refresh tokens with a limited lifetime (currently 7 days) whatever the scope, so
the user re-runs `cwops auth` from time to time.
"""

from __future__ import annotations

import json
import logging
import os
import stat
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from ..config import DRIVE_FILE_SCOPE, Settings
from ..errors import ConfigurationError
from ..logging import get_logger, log_event

logger = get_logger("drive.auth")


def _assert_least_privilege(credentials: Credentials) -> None:
    granted = set(credentials.scopes or [])
    extra = granted - {DRIVE_FILE_SCOPE}
    if extra:
        raise ConfigurationError(
            "Credentials carry scopes beyond drive.file: "
            f"{sorted(extra)}. Delete the token file and re-authenticate.",
            unexpected_scopes=sorted(extra),
        )


def _save(credentials: Credentials, token_path: Path) -> None:
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(credentials.to_json(), encoding="utf-8")
    try:
        # 0600. On Windows this only clears the read-only attribute; the file's
        # real protection there is the user-profile ACL it inherits.
        os.chmod(token_path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:  # pragma: no cover - best effort on some filesystems
        log_event(logger, logging.WARNING, "auth.chmod_failed", path=str(token_path))


def load_credentials(settings: Settings, allow_interactive: bool = True) -> Credentials:
    """Return usable credentials, running the consent flow only if needed."""
    token_path = settings.token_path.expanduser()
    credentials: Credentials | None = None

    if token_path.is_file():
        try:
            credentials = Credentials.from_authorized_user_file(
                str(token_path), settings.scopes
            )
        except (ValueError, json.JSONDecodeError) as exc:
            raise ConfigurationError(
                f"Stored token at {token_path} is unreadable; delete it and re-run "
                "`cwops auth`.",
            ) from exc

    if credentials and credentials.valid:
        _assert_least_privilege(credentials)
        return credentials

    if credentials and credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
        _assert_least_privilege(credentials)
        _save(credentials, token_path)
        log_event(logger, logging.INFO, "auth.refreshed", scopes=settings.scopes)
        return credentials

    if not allow_interactive:
        raise ConfigurationError(
            "No usable Google credentials. Run `cwops auth` to authorize.",
            token_path=str(token_path),
        )

    flow = InstalledAppFlow.from_client_secrets_file(
        str(settings.require_google_credentials()), settings.scopes
    )
    credentials = flow.run_local_server(port=0)
    _assert_least_privilege(credentials)
    _save(credentials, token_path)
    log_event(logger, logging.INFO, "auth.authorized", scopes=settings.scopes)
    return credentials
