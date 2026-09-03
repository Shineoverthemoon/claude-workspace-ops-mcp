"""Read-only means read-only, and dry-run means nothing was written.

These assert against the fake Drive's recorded call log rather than against
intent, so a future tool that quietly mutates would fail here.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from conftest import ROOT_ID, ToolCaller
from cwops.container import Container
from cwops.drive import FakeDriveClient

SOURCE_DIR = Path(__file__).resolve().parents[1] / "src" / "cwops"


def test_every_read_tool_performs_zero_mutations(
    call: ToolCaller, drive: FakeDriveClient
) -> None:
    call("drive_search", query="invoice")
    call("drive_list_folder", folder_id=ROOT_ID, recursive=True)
    call("drive_read_file", file_id="f-notes")
    call("find_duplicates", folder_id="fol-inbox")
    call("classify_documents", file_ids=["f-inv-q1", "f-notes"])
    assert drive.mutating_calls == []


def test_proposing_performs_zero_mutations(call: ToolCaller, drive: FakeDriveClient) -> None:
    proposal = call("propose_organization", folder_id="fol-inbox")
    assert proposal["ops"]
    assert drive.mutating_calls == []


def test_dry_run_apply_performs_zero_mutations(
    call: ToolCaller, drive: FakeDriveClient
) -> None:
    proposal = call("propose_organization", folder_id="fol-inbox")
    before = drive.snapshot()

    result = call("apply_proposal", proposal_id=proposal["id"])

    assert result["dry_run"] is True
    assert result["applied"] is False
    assert result["executed"] == 0
    assert {row["status"] for row in result["results"]} == {"dry_run"}
    assert drive.mutating_calls == []
    assert drive.snapshot() == before


def test_dry_run_is_the_default(call: ToolCaller) -> None:
    proposal = call("propose_organization", folder_id="fol-inbox")
    assert call("apply_proposal", proposal_id=proposal["id"])["dry_run"] is True


def test_dry_run_reports_whether_approval_is_present(call: ToolCaller) -> None:
    proposal = call("propose_organization", folder_id="fol-inbox")
    result = call("apply_proposal", proposal_id=proposal["id"])
    assert result["approval"]["approved"] is False


def test_dry_run_works_even_when_mutations_are_disabled(
    make_container: object, drive: FakeDriveClient
) -> None:
    from conftest import make_caller

    container: Container = make_container(allow_mutations=False)  # type: ignore[operator]
    try:
        call = make_caller(container)
        proposal = call("propose_organization", folder_id="fol-inbox")
        assert call("apply_proposal", proposal_id=proposal["id"])["dry_run"] is True
        assert drive.mutating_calls == []
    finally:
        container.close()


def test_a_real_run_without_approval_changes_nothing(
    call: ToolCaller, drive: FakeDriveClient
) -> None:
    proposal = call("propose_organization", folder_id="fol-inbox")
    before = drive.snapshot()
    with pytest.raises(ToolError) as exc:
        call("apply_proposal", proposal_id=proposal["id"], dry_run=False)
    assert "approval_required" in str(exc.value)
    assert drive.mutating_calls == []
    assert drive.snapshot() == before


#: The only modules allowed to call a mutating Drive method.
#: - drive/ holds the implementations themselves.
#: - tools/apply.py is the single agent-reachable write path (I1).
#: - provision.py is human-initiated workspace setup, CLI-only, never a tool.
MUTATION_ALLOWLIST = {"apply.py", "provision.py"}


def test_only_the_allowlisted_modules_touch_mutating_drive_methods() -> None:
    """Architecture test for I1: one write path, verified in the source itself."""
    offenders: list[str] = []
    for path in SOURCE_DIR.rglob("*.py"):
        if path.parent.name == "drive" or path.name in MUTATION_ALLOWLIST:
            continue
        text = path.read_text(encoding="utf-8")
        for method in (
            "drive.create_folder(",
            "drive.rename(",
            "drive.move(",
            "drive.create_text_file(",
        ):
            if method in text:
                offenders.append(f"{path.name}: {method}")
    assert offenders == [], f"Unexpected mutating Drive calls: {offenders}"


def test_the_agent_reachable_write_path_is_apply_only() -> None:
    """provision.py is CLI-only: no tool module may import it."""
    tools_dir = SOURCE_DIR / "tools"
    for path in tools_dir.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "provision" not in text, f"{path.name} reaches provisioning code"
