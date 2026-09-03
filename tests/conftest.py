from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from cwops.ai import FakeOrganizer, Organizer
from cwops.clock import FixedClock
from cwops.config import Settings
from cwops.container import Container, build_container
from cwops.drive import FakeDriveClient
from cwops.drive.fake import SEED_PATH
from cwops.models import DriveFile
from cwops.rules import ValidationContext
from cwops.server import build_server
from cwops.store import AuditLog, Database, GrantLedger, ProposalStore

FIXTURES = Path(__file__).parent / "fixtures"

# Keep the ambient environment from leaking into tests: every CWOPS_* value used
# by a test must be explicit.
_CWOPS_PREFIX = "CWOPS_"

ROOT_ID = "root-workspace"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.startswith(_CWOPS_PREFIX):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock()


@pytest.fixture
def sleeps() -> list[float]:
    """Collects backoff durations instead of actually sleeping."""
    return []


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = Database(tmp_path / "cwops.db")
    yield database
    database.close()


@pytest.fixture
def audit(db: Database, clock: FixedClock) -> AuditLog:
    return AuditLog(db, clock)


@pytest.fixture
def grants(db: Database, clock: FixedClock) -> GrantLedger:
    return GrantLedger(db, clock)


@pytest.fixture
def proposals(db: Database, clock: FixedClock) -> ProposalStore:
    return ProposalStore(db, clock, approval_ttl_seconds=900)


@pytest.fixture
def drive() -> FakeDriveClient:
    return FakeDriveClient.from_seed()


@pytest.fixture
def seed() -> dict[str, Any]:
    return json.loads(SEED_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def known_files(seed: dict[str, Any]) -> dict[str, DriveFile]:
    """The snapshot a plan is validated against - every file, granted or not."""
    return {entry["id"]: DriveFile.model_validate(entry) for entry in seed["files"]}


@pytest.fixture
def granted_ids(seed: dict[str, Any]) -> set[str]:
    return set(seed["granted"])


@pytest.fixture
def vctx(known_files: dict[str, DriveFile], granted_ids: set[str]) -> ValidationContext:
    return ValidationContext(
        root_folder_id=ROOT_ID,
        known_files=known_files,
        granted_ids=granted_ids,
        max_ops=50,
    )


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        drive_backend="fake",
        ai_backend="fake",
        allow_mutations=True,
        root_folder_id=ROOT_ID,
        db_path=tmp_path / "cwops.db",
        _env_file=None,  # type: ignore[call-arg]
    )


# --- wired system ---------------------------------------------------------


@pytest.fixture
def make_container(
    tmp_path: Path, clock: FixedClock, drive: FakeDriveClient
) -> Callable[..., Container]:
    def _make(
        allow_mutations: bool = True,
        organizer: Organizer | None = None,
        **overrides: Any,
    ) -> Container:
        settings = Settings(
            drive_backend="fake",
            ai_backend="fake",
            allow_mutations=allow_mutations,
            root_folder_id=ROOT_ID,
            db_path=tmp_path / "cwops.db",
            _env_file=None,  # type: ignore[call-arg]
            **overrides,
        )
        return build_container(
            settings,
            clock=clock,
            drive=drive,
            organizer=organizer or FakeOrganizer(),
        )

    return _make


@pytest.fixture
def container(make_container: Callable[..., Container]) -> Iterator[Container]:
    built = make_container()
    yield built
    built.close()


ToolCaller = Callable[..., Any]


def make_caller(container: Container) -> ToolCaller:
    """Invoke a registered MCP tool by name and return its structured payload."""
    server = build_server(container)

    def _call(tool_name: str, **arguments: Any) -> Any:
        result = asyncio.run(server.call_tool(tool_name, arguments))
        payload = result.structured_content
        if isinstance(payload, dict) and set(payload) == {"result"}:
            return payload["result"]
        return payload

    return _call


@pytest.fixture
def call(container: Container) -> ToolCaller:
    return make_caller(container)
