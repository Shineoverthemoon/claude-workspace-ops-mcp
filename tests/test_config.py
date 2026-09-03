from __future__ import annotations

import pytest

from cwops.config import DRIVE_FILE_SCOPE, SCOPES, Settings, load_settings
from cwops.errors import ConfigurationError
from cwops.logging import REDACTED, scrub


def test_only_drive_file_scope_is_requested() -> None:
    """Least privilege is a hard-coded property, not a configurable one."""
    assert SCOPES == (DRIVE_FILE_SCOPE,)
    assert DRIVE_FILE_SCOPE == "https://www.googleapis.com/auth/drive.file"
    assert Settings(_env_file=None).scopes == [DRIVE_FILE_SCOPE]  # type: ignore[call-arg]


def test_defaults_are_safe() -> None:
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.allow_mutations is False
    assert s.drive_backend == "fake"
    assert s.ai_backend == "fake"


def test_mutations_without_workspace_fail_fast() -> None:
    with pytest.raises(ConfigurationError) as exc:
        load_settings(allow_mutations=True, root_folder_id="  ", _env_file=None)
    assert "CWOPS_ROOT_FOLDER_ID" in str(exc.value)


def test_mutations_with_workspace_ok() -> None:
    s = load_settings(allow_mutations=True, root_folder_id="folder-1", _env_file=None)
    assert s.require_root_folder() == "folder-1"


def test_require_root_folder_raises_when_unset() -> None:
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    with pytest.raises(ConfigurationError):
        s.require_root_folder()


@pytest.mark.parametrize(
    "payload",
    [
        {"access_token": "ya29.a0AfH6"},
        {"ANTHROPIC_API_KEY": "sk-ant-123"},
        {"nested": {"refresh_token": "1//0gabc"}},
        {"client_secret": "abc"},
    ],
)
def test_scrub_redacts_secret_keys(payload: dict[str, object]) -> None:
    scrubbed = scrub(payload)
    flat = str(scrubbed)
    assert REDACTED in flat
    assert "ya29.a0AfH6" not in flat
    assert "sk-ant-123" not in flat


def test_scrub_redacts_secret_shaped_values_under_innocent_keys() -> None:
    scrubbed = scrub({"note": "sk-ant-abc123", "name": "quarterly report"})
    assert scrubbed["note"] == REDACTED
    assert scrubbed["name"] == "quarterly report"


def test_scrub_preserves_ordinary_data() -> None:
    payload = {"file_id": "abc", "count": 3, "names": ["a", "b"]}
    assert scrub(payload) == payload
