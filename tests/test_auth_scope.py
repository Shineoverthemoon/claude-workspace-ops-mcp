"""OAuth least privilege, checked without touching the network.

drive.file is requested in exactly one place and asserted on the way back in.
A token that somehow carries broader access is a configuration error, not a
convenience.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cwops.config import DRIVE_FILE_SCOPE, Settings
from cwops.drive import auth
from cwops.errors import ConfigurationError

FULL_DRIVE = "https://www.googleapis.com/auth/drive"


def settings_with(tmp_path: Path, token_name: str = "token.json") -> Settings:
    return Settings(
        drive_backend="google",
        token_path=tmp_path / token_name,
        google_client_secrets=tmp_path / "client_secret.json",
        _env_file=None,  # type: ignore[call-arg]
    )


def test_only_drive_file_is_ever_requested(tmp_path: Path) -> None:
    assert settings_with(tmp_path).scopes == [DRIVE_FILE_SCOPE]


def test_a_token_with_exactly_drive_file_is_accepted() -> None:
    auth._assert_least_privilege(SimpleNamespace(scopes=[DRIVE_FILE_SCOPE]))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "scopes",
    [
        [FULL_DRIVE],
        [DRIVE_FILE_SCOPE, FULL_DRIVE],
        [DRIVE_FILE_SCOPE, "https://www.googleapis.com/auth/drive.readonly"],
    ],
)
def test_a_token_carrying_broader_scope_is_refused(scopes: list[str]) -> None:
    """Extra access is a defect to reject, never a bonus to accept."""
    with pytest.raises(ConfigurationError) as exc:
        auth._assert_least_privilege(SimpleNamespace(scopes=scopes))  # type: ignore[arg-type]
    assert "beyond drive.file" in exc.value.detail
    assert FULL_DRIVE in str(exc.value.context["unexpected_scopes"]) or scopes[-1] in str(
        exc.value.context["unexpected_scopes"]
    )


def test_missing_credentials_fail_closed_in_non_interactive_mode(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError) as exc:
        auth.load_credentials(settings_with(tmp_path), allow_interactive=False)
    assert "cwops auth" in exc.value.detail


def test_a_corrupt_token_file_is_reported_clearly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = tmp_path / "token.json"
    token.write_text("{not json", encoding="utf-8")
    with pytest.raises(ConfigurationError) as exc:
        auth.load_credentials(settings_with(tmp_path), allow_interactive=False)
    assert "unreadable" in exc.value.detail


def test_a_valid_stored_token_is_reused_without_a_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = tmp_path / "token.json"
    token.write_text("{}", encoding="utf-8")
    stored = SimpleNamespace(valid=True, scopes=[DRIVE_FILE_SCOPE], expired=False)

    def from_file(path: str, scopes: list[str]) -> Any:
        assert scopes == [DRIVE_FILE_SCOPE]
        return stored

    monkeypatch.setattr(auth.Credentials, "from_authorized_user_file", staticmethod(from_file))
    assert auth.load_credentials(settings_with(tmp_path), allow_interactive=False) is stored


def test_the_token_file_is_created_with_owner_only_permissions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Asserted via the chmod call: POSIX mode bits are a no-op on Windows."""
    chmod_calls: list[tuple[Any, int]] = []
    monkeypatch.setattr(auth.os, "chmod", lambda path, mode: chmod_calls.append((path, mode)))

    token = tmp_path / "nested" / "token.json"
    auth._save(SimpleNamespace(to_json=lambda: '{"x": 1}'), token)  # type: ignore[arg-type]

    assert token.read_text(encoding="utf-8") == '{"x": 1}'
    assert chmod_calls == [(token, 0o600)]


@pytest.mark.skipif(not hasattr(__import__("os"), "getuid"), reason="POSIX-only mode bits")
def test_the_token_file_has_no_group_or_other_access_on_posix(tmp_path: Path) -> None:
    token = tmp_path / "token.json"
    auth._save(SimpleNamespace(to_json=lambda: "{}"), token)  # type: ignore[arg-type]
    assert token.stat().st_mode & 0o077 == 0


def test_client_secrets_missing_is_an_actionable_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError) as exc:
        settings_with(tmp_path).require_google_credentials()
    assert exc.value.context["env_var"] == "CWOPS_GOOGLE_CLIENT_SECRETS"


def test_auth_can_bootstrap_before_the_google_backend_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Regression: `cwops auth` must not need a container.

    Building the Google backend requires working credentials, so routing auth
    through the container would deadlock on the very first run.
    """
    from cwops.cli import main

    monkeypatch.setenv("CWOPS_DRIVE_BACKEND", "google")
    monkeypatch.setenv("CWOPS_TOKEN_PATH", str(tmp_path / "token.json"))
    monkeypatch.setenv("CWOPS_GOOGLE_CLIENT_SECRETS", str(tmp_path / "client_secret.json"))
    monkeypatch.setattr(
        "cwops.drive.auth.load_credentials",
        lambda settings, allow_interactive=True: SimpleNamespace(scopes=[DRIVE_FILE_SCOPE]),
    )

    assert main(["auth"]) == 0
    assert DRIVE_FILE_SCOPE in capsys.readouterr().out
