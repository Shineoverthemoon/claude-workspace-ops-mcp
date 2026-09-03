"""The model is advisory. These tests prove it structurally.

The claim being defended: even a fully compromised Organizer - one returning
whatever an attacker wants - cannot cause an unsafe operation, because the shape
it answers in cannot express one and the validator re-checks what it does say.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from pydantic import ValidationError

from conftest import ROOT_ID, ToolCaller, make_caller
from cwops.ai import FakeOrganizer
from cwops.container import Container
from cwops.models import DraftAssignment, DraftFolder, DraftPlan


def hostile_caller(
    make_container: Callable[..., Container], plan: DraftPlan
) -> tuple[ToolCaller, Container]:
    container = make_container(organizer=FakeOrganizer.hostile(plan))
    return make_caller(container), container


# --- the schema itself is a control ---------------------------------------


def test_a_draft_plan_cannot_express_a_rename_or_a_delete() -> None:
    """There is no field for it. The model has no vocabulary for these actions."""
    assert set(DraftPlan.model_fields) == {"folders", "assignments", "rationale"}
    assert set(DraftFolder.model_fields) == {"ref", "name"}
    assert set(DraftAssignment.model_fields) == {"file_id", "folder_ref"}


def test_a_draft_plan_cannot_name_a_destination_outside_the_workspace() -> None:
    """Assignments point at plan-local refs only - never at a Drive folder ID."""
    with pytest.raises(ValidationError):
        DraftAssignment(file_id="f-inv-q1", folder_ref="some-other-drive-folder")


@pytest.mark.parametrize("ref", ["../escape", "#UPPER", "#", "not-a-ref", "#" + "x" * 41])
def test_malformed_refs_are_rejected_at_the_schema_boundary(ref: str) -> None:
    with pytest.raises(ValidationError):
        DraftFolder(ref=ref, name="Anything")


def test_folder_count_is_capped_by_the_schema() -> None:
    with pytest.raises(ValidationError):
        DraftPlan(folders=[DraftFolder(ref=f"#f{i}", name=f"F{i}") for i in range(26)])


# --- a hostile organizer changes nothing ----------------------------------


def test_hallucinated_file_ids_are_rejected_by_the_validator(
    make_container: Callable[..., Container],
) -> None:
    plan = DraftPlan(
        folders=[DraftFolder(ref="#loot", name="Loot")],
        assignments=[
            DraftAssignment(file_id="f-does-not-exist", folder_ref="#loot"),
            DraftAssignment(file_id="../../etc/passwd", folder_ref="#loot"),
        ],
        rationale="trust me",
    )
    call, container = hostile_caller(make_container, plan)
    try:
        proposal = call("propose_organization", folder_id="fol-inbox")
        reasons = {r["reason_code"] for r in proposal["validation"]["rejected"]}
        assert reasons == {"unknown_file"}
        # Only the folder creation survived; no file was assigned anywhere.
        assert [op["op"] for op in proposal["ops"]] == ["create_folder"]
    finally:
        container.close()


def test_a_hostile_plan_targeting_an_ungranted_file_is_rejected(
    make_container: Callable[..., Container],
) -> None:
    plan = DraftPlan(
        folders=[DraftFolder(ref="#hr", name="HR")],
        assignments=[DraftAssignment(file_id="f-not-granted", folder_ref="#hr")],
    )
    call, container = hostile_caller(make_container, plan)
    try:
        proposal = call("propose_organization", folder_id="fol-inbox")
        reasons = {r["reason_code"] for r in proposal["validation"]["rejected"]}
        assert reasons <= {"unknown_file", "not_granted"}
        assert not any(op["op"] == "move" for op in proposal["ops"])
    finally:
        container.close()


def test_a_hostile_plan_cannot_reach_a_file_outside_the_workspace(
    make_container: Callable[..., Container],
) -> None:
    """The file is genuinely reachable under drive.file, and still refused."""
    plan = DraftPlan(
        folders=[DraftFolder(ref="#steal", name="Steal")],
        assignments=[DraftAssignment(file_id="f-outside-workspace", folder_ref="#steal")],
    )
    call, container = hostile_caller(make_container, plan)
    try:
        proposal = call("propose_organization", folder_id="fol-inbox")
        assert not any(op["op"] == "move" for op in proposal["ops"])
    finally:
        container.close()


def test_created_folders_are_always_parented_to_the_workspace_root(
    make_container: Callable[..., Container],
) -> None:
    """The parent comes from configuration, never from the model."""
    plan = DraftPlan(folders=[DraftFolder(ref="#anywhere", name="Anywhere")])
    call, container = hostile_caller(make_container, plan)
    try:
        proposal = call("propose_organization", folder_id="fol-inbox")
        creates = [op for op in proposal["ops"] if op["op"] == "create_folder"]
        assert creates and all(op["parent_id"] == ROOT_ID for op in creates)
    finally:
        container.close()


def test_a_wholly_invalid_plan_cannot_be_approved(
    make_container: Callable[..., Container],
) -> None:
    from cwops.errors import PlanRejected

    plan = DraftPlan(
        assignments=[DraftAssignment(file_id="f-nope", folder_ref="#ghost")],
    )
    call, container = hostile_caller(make_container, plan)
    try:
        proposal = call("propose_organization", folder_id="fol-inbox")
        assert proposal["validation"]["ok"] is False
        with pytest.raises(PlanRejected):
            container.proposals.approve(proposal["id"], "operator")
    finally:
        container.close()


# --- classification results are filtered ----------------------------------


def test_classifications_for_unrequested_files_are_discarded(call: ToolCaller) -> None:
    results = call("classify_documents", file_ids=["f-inv-q1"])
    assert [item["file_id"] for item in results] == ["f-inv-q1"]


def test_classification_is_advisory_only(call: ToolCaller) -> None:
    """A label carries no authority: nothing in the result can trigger an action."""
    results = call("classify_documents", file_ids=["f-inv-q1", "f-notes"])
    for item in results:
        assert set(item) == {"file_id", "category", "topics", "confidence", "rationale"}
        assert 0.0 <= item["confidence"] <= 1.0


def test_the_offline_organizer_produces_a_usable_plan(call: ToolCaller) -> None:
    """Demo mode must still exercise the whole pipeline end to end."""
    proposal = call("propose_organization", folder_id="fol-inbox")
    assert proposal["validation"]["ok"] is True
    assert proposal["validation"]["rejected"] == []
    assert any(op["op"] == "create_folder" for op in proposal["ops"])
    assert any(op["op"] == "move" for op in proposal["ops"])
    assert proposal["preview"]
