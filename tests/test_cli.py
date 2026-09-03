"""The operator CLI - the trusted half of the approval gate."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import ROOT_ID, ToolCaller
from cwops.cli import main
from cwops.container import Container


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the CLI at the same throwaway database the fixtures use."""
    db_path = tmp_path / "cwops.db"
    monkeypatch.setenv("CWOPS_DRIVE_BACKEND", "fake")
    monkeypatch.setenv("CWOPS_AI_BACKEND", "fake")
    monkeypatch.setenv("CWOPS_ALLOW_MUTATIONS", "true")
    monkeypatch.setenv("CWOPS_ROOT_FOLDER_ID", ROOT_ID)
    monkeypatch.setenv("CWOPS_DB_PATH", str(db_path))
    return db_path


def a_proposal(call: ToolCaller) -> str:
    proposal = call(
        "propose_operations",
        ops=[{"op": "rename", "file_id": "f-inv-q1", "new_name": "Invoice 2026-Q1.pdf"}],
    )
    assert proposal["validation"]["ok"] is True
    return str(proposal["id"])


def test_demo_runs_the_whole_pipeline_offline(capsys: pytest.CaptureFixture[str]) -> None:
    """The README's walkthrough is executable, so it cannot rot."""
    assert main(["demo"]) == 0
    output = capsys.readouterr().out
    assert "drive.file scope only" in output
    assert "Refused:" in output  # the unapproved attempt
    assert "applied=True" in output
    assert "audit rows" in output


def test_approve_creates_an_approval_and_an_audit_row(
    cli_env: Path, call: ToolCaller, container: Container, capsys: pytest.CaptureFixture[str]
) -> None:
    proposal_id = a_proposal(call)
    assert container.proposals.latest_approval(proposal_id) is None

    assert main(["approve", proposal_id, "--yes", "--approver", "alice"]) == 0

    approval = container.proposals.latest_approval(proposal_id)
    assert approval is not None
    assert approval.approver == "alice"
    rows = [row for row in container.audit.for_proposal(proposal_id) if row.actor == "cli"]
    assert rows[-1].approved_by == "alice"
    assert rows[-1].correlation_id != "-"
    assert "Approved by alice" in capsys.readouterr().out


def test_approve_shows_the_preview_before_asking(
    cli_env: Path, call: ToolCaller, capsys: pytest.CaptureFixture[str]
) -> None:
    proposal_id = a_proposal(call)
    main(["approve", proposal_id, "--yes"])
    output = capsys.readouterr().out
    assert "RENAME" in output
    assert "Invoice Q1 2026.pdf" in output


def test_approve_declined_at_the_prompt_creates_nothing(
    cli_env: Path,
    call: ToolCaller,
    container: Container,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proposal_id = a_proposal(call)
    monkeypatch.setattr("builtins.input", lambda *_: "n")
    assert main(["approve", proposal_id]) == 1
    assert container.proposals.latest_approval(proposal_id) is None


def test_approve_refuses_a_plan_that_failed_validation(
    cli_env: Path, call: ToolCaller, capsys: pytest.CaptureFixture[str]
) -> None:
    proposal = call(
        "propose_operations",
        ops=[{"op": "rename", "file_id": "f-hallucinated", "new_name": "x.pdf"}],
    )
    assert main(["approve", proposal["id"], "--yes"]) == 2
    assert "plan_rejected" in capsys.readouterr().err


def test_approve_reports_an_unknown_proposal_clearly(
    cli_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["approve", "prop_nope", "--yes"]) == 2
    assert "proposal_not_found" in capsys.readouterr().err


def test_list_and_audit_render(
    cli_env: Path, call: ToolCaller, capsys: pytest.CaptureFixture[str]
) -> None:
    proposal_id = a_proposal(call)
    assert main(["list"]) == 0
    assert proposal_id in capsys.readouterr().out
    assert main(["audit", "--limit", "5"]) == 0
    assert "propose_operations" in capsys.readouterr().out


def test_audit_export_writes_jsonl(
    cli_env: Path, call: ToolCaller, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    a_proposal(call)
    target = tmp_path / "audit.jsonl"
    assert main(["audit", "--export", str(target)]) == 0
    assert target.read_text(encoding="utf-8").strip().splitlines()


def test_grants_lists_only_reachable_files(
    cli_env: Path, call: ToolCaller, capsys: pytest.CaptureFixture[str]
) -> None:
    call("drive_list_folder", folder_id="fol-inbox")
    assert main(["grants"]) == 0
    output = capsys.readouterr().out
    assert "f-inv-q1" in output
    assert "f-not-granted" not in output


def test_grant_explains_how_drive_file_access_is_obtained(
    cli_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["grant", "f-not-granted"]) == 1
    error = capsys.readouterr().err
    assert "drive.file" in error
    assert "opened/shared" in error


def test_grant_accepts_a_drive_url(cli_env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["grant", "https://drive.google.com/file/d/f-inv-q1"]) == 0
    assert "Verified access: f-inv-q1" in capsys.readouterr().out


def test_approve_exists_on_the_cli_and_nowhere_else(container: Container) -> None:
    """The two halves of the gate live in different processes, by construction."""
    import asyncio

    from cwops.cli import build_parser
    from cwops.server import build_server

    parser = build_parser()
    assert parser.parse_args(["approve", "prop_x", "-y"]).command == "approve"

    tool_names = {tool.name for tool in asyncio.run(build_server(container).list_tools())}
    assert "approve" not in tool_names
