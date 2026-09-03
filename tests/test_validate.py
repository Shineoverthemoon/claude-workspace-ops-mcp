"""Adversarial validation tests.

The premise: a plan is untrusted input no matter who wrote it. Most cases come
from tests/fixtures/adversarial_plans.json, which is written as plans a
compromised or hallucinating model might realistically emit.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import TypeAdapter

from conftest import FIXTURES, ROOT_ID
from cwops.errors import LimitExceeded
from cwops.models import CreateFolder, DriveFile, Move, Operation, Rename
from cwops.rules import ValidationContext, render_preview, validate_plan
from cwops.rules import validate as V

_OPS = TypeAdapter(list[Operation])

ADVERSARIAL: list[dict[str, Any]] = json.loads(
    (FIXTURES / "adversarial_plans.json").read_text(encoding="utf-8")
)["cases"]


@pytest.mark.parametrize("case", ADVERSARIAL, ids=lambda c: str(c["name"]))
def test_adversarial_plans_are_rejected(case: dict[str, Any], vctx: ValidationContext) -> None:
    ops = _OPS.validate_python(case["ops"])
    accepted, report = validate_plan(ops, vctx)

    reasons = {rejection.reason_code for rejection in report.rejected}
    assert case["expect"] in reasons, f"{case['name']}: got {reasons or 'no rejections'}"

    # The offending operation must not survive into the accepted list.
    assert len(accepted) == len(ops) - len(report.rejected)


def test_every_reason_code_in_the_fixture_is_a_declared_constant() -> None:
    declared = {
        value
        for name, value in vars(V).items()
        if name.isupper() and isinstance(value, str) and not name.startswith("_")
    }
    assert {case["expect"] for case in ADVERSARIAL} <= declared


# --- accepting good plans -------------------------------------------------


def test_a_clean_plan_is_fully_accepted(vctx: ValidationContext) -> None:
    ops: list[Operation] = [
        CreateFolder(ref="#invoices", name="Invoices", parent_id=ROOT_ID),
        Move(file_id="f-inv-q1", new_parent_id="#invoices"),
        Rename(file_id="f-inv-q2", new_name="Invoice 2026-Q2.pdf"),
    ]
    accepted, report = validate_plan(ops, vctx)
    assert report.ok is True
    assert report.rejected == []
    assert len(accepted) == 3


def test_nested_refs_resolve(vctx: ValidationContext) -> None:
    ops: list[Operation] = [
        CreateFolder(ref="#finance", name="Finance", parent_id=ROOT_ID),
        CreateFolder(ref="#invoices", name="Invoices", parent_id="#finance"),
        Move(file_id="f-inv-q1", new_parent_id="#invoices"),
    ]
    _, report = validate_plan(ops, vctx)
    assert report.rejected == []


def test_moving_a_folder_into_a_sibling_is_allowed(vctx: ValidationContext) -> None:
    _, report = validate_plan([Move(file_id="fol-archive-2025", new_parent_id="fol-inbox")], vctx)
    assert report.rejected == []


# --- partial acceptance ---------------------------------------------------


def test_bad_operations_are_dropped_and_good_ones_survive_in_order(
    vctx: ValidationContext,
) -> None:
    ops: list[Operation] = [
        Rename(file_id="f-inv-q1", new_name="Keep me.pdf"),
        Rename(file_id="f-hallucinated", new_name="Drop me.pdf"),
        Rename(file_id="f-inv-q2", new_name="Keep me too.pdf"),
    ]
    accepted, report = validate_plan(ops, vctx)
    assert [op.file_id for op in accepted] == ["f-inv-q1", "f-inv-q2"]  # type: ignore[union-attr]
    assert report.ok is True
    assert report.accepted == 2
    assert report.rejected[0].index == 1
    assert report.rejected[0].reason_code == V.UNKNOWN_FILE


def test_a_plan_with_nothing_left_is_not_ok(vctx: ValidationContext) -> None:
    _, report = validate_plan([Rename(file_id="nope", new_name="x.pdf")], vctx)
    assert report.ok is False
    assert report.accepted == 0


def test_rejections_carry_a_readable_summary(vctx: ValidationContext) -> None:
    _, report = validate_plan([Rename(file_id="nope", new_name="x.pdf")], vctx)
    assert "rename nope" in report.rejected[0].summary
    assert report.rejected[0].detail


# --- limits ---------------------------------------------------------------


def test_oversized_plans_are_refused_outright(vctx: ValidationContext) -> None:
    vctx.max_ops = 3
    ops: list[Operation] = [
        CreateFolder(ref=f"#f{i}", name=f"F{i}", parent_id=ROOT_ID) for i in range(4)
    ]
    with pytest.raises(LimitExceeded) as exc:
        validate_plan(ops, vctx)
    assert exc.value.context["limit"] == 3


# --- containment ----------------------------------------------------------


def test_a_fully_known_chain_to_a_different_root_is_outside_the_workspace() -> None:
    """Distinct from 'unverifiable': here containment is positively disproved."""
    other_root = DriveFile(
        id="other-root", name="Other", mime_type="application/vnd.google-apps.folder"
    )
    stray = DriveFile(
        id="stray", name="Stray.pdf", mime_type="application/pdf", parents=["other-root"]
    )
    ctx = ValidationContext(
        root_folder_id=ROOT_ID,
        known_files={"other-root": other_root, "stray": stray},
        granted_ids={"other-root", "stray"},
    )
    _, report = validate_plan([Rename(file_id="stray", new_name="Mine.pdf")], ctx)
    assert report.rejected[0].reason_code == V.OUTSIDE_WORKSPACE


def test_grants_alone_do_not_authorize_a_file_outside_the_workspace(
    vctx: ValidationContext,
) -> None:
    """Least privilege and blast radius are independent controls, and both apply."""
    assert "f-outside-workspace" in vctx.granted_ids
    _, report = validate_plan([Rename(file_id="f-outside-workspace", new_name="x")], vctx)
    assert report.ok is False


# --- preview --------------------------------------------------------------


def test_preview_uses_names_not_ids(vctx: ValidationContext) -> None:
    ops: list[Operation] = [
        CreateFolder(ref="#invoices", name="Invoices", parent_id=ROOT_ID),
        Move(file_id="f-inv-q1", new_parent_id="#invoices"),
        Rename(file_id="f-inv-q2", new_name="Invoice 2026-Q2.pdf"),
    ]
    lines = render_preview(ops, vctx.known_files)
    assert lines[0].startswith("CREATE FOLDER")
    assert "'Invoices'" in lines[0]
    assert "'Invoice Q1 2026.pdf'" in lines[1]
    assert "'Invoices' (new)" in lines[1]
    assert "'Invoice Q1 2026.pdf'" not in lines[2]
    assert "f-inv-q1" not in "".join(lines)
