"""The published MCP tool contract.

The golden snapshot makes a schema change a visible, reviewed diff rather than a
silent break for any client already wired to this server. Regenerate with:

    UPDATE_TOOL_SCHEMAS=1 pytest tests/test_tool_schemas.py
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from conftest import FIXTURES
from cwops.container import Container
from cwops.server import build_server

GOLDEN = FIXTURES / "tool_schemas.json"

EXPECTED_TOOLS = {
    "drive_search",
    "drive_list_folder",
    "drive_read_file",
    "find_duplicates",
    "classify_documents",
    "propose_organization",
    "propose_operations",
    "get_proposal",
    "apply_proposal",
}

READ_ONLY_TOOLS = EXPECTED_TOOLS - {"apply_proposal"}


def tool_index(container: Container) -> dict[str, Any]:
    server = build_server(container)
    tools = asyncio.run(server.list_tools())
    return {
        tool.name: {
            "description": tool.description,
            "input_schema": tool.input_schema,
            "output_schema": tool.output_schema,
        }
        for tool in tools
    }


def test_exactly_the_expected_tools_are_published(container: Container) -> None:
    assert set(tool_index(container)) == EXPECTED_TOOLS


def test_schemas_match_the_golden_snapshot(container: Container) -> None:
    current = tool_index(container)
    # Not CWOPS_-prefixed on purpose: conftest strips those to isolate settings.
    if os.environ.get("UPDATE_TOOL_SCHEMAS"):
        GOLDEN.write_text(json.dumps(current, indent=2, sort_keys=True), encoding="utf-8")
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert current == golden, (
        "Published tool schemas changed. If intentional, regenerate with "
        "UPDATE_TOOL_SCHEMAS=1 pytest tests/test_tool_schemas.py"
    )


def test_every_tool_declares_a_description_and_output_schema(container: Container) -> None:
    for name, spec in tool_index(container).items():
        assert spec["description"], f"{name} has no description"
        assert spec["output_schema"], f"{name} has no output schema"


def test_apply_proposal_defaults_to_dry_run(container: Container) -> None:
    schema = tool_index(container)["apply_proposal"]["input_schema"]
    assert schema["properties"]["dry_run"]["default"] is True
    assert schema["required"] == ["proposal_id"]


def test_apply_proposal_is_advertised_as_the_only_write_path(container: Container) -> None:
    description = tool_index(container)["apply_proposal"]["description"]
    assert "ONLY tool" in description
    assert "cwops approve" in description


def test_no_destructive_verb_is_exposed_anywhere(container: Container) -> None:
    """There is no delete/trash/share capability to call, not merely a rule against it."""
    index = tool_index(container)
    assert not any(
        verb in name for name in index for verb in ("delete", "trash", "remove", "share")
    )
    for name, spec in index.items():
        blob = json.dumps(spec["input_schema"])
        assert "permission" not in blob.lower(), name


def test_least_privilege_is_stated_in_the_read_tool_contract(container: Container) -> None:
    description = tool_index(container)["drive_search"]["description"]
    assert "created" in description and "shared" in description


def test_operation_union_is_discriminated_in_the_published_schema(
    container: Container,
) -> None:
    schema = tool_index(container)["propose_operations"]["input_schema"]
    blob = json.dumps(schema)
    for op in ("create_folder", "rename", "move"):
        assert op in blob
    assert "discriminator" in blob


def test_read_tools_take_no_mutation_flags(container: Container) -> None:
    index = tool_index(container)
    for name in READ_ONLY_TOOLS:
        properties = index[name]["input_schema"].get("properties", {})
        assert "dry_run" not in properties, name
