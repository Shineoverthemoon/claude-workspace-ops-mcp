"""MCP server entry point. Wiring only - no business logic lives here.

Transport is stdio, which means STDOUT CARRIES THE JSON-RPC STREAM. All logging
goes to stderr (see cwops.logging); a stray print() here would corrupt the
protocol.
"""

from __future__ import annotations

import logging
import sys

from mcp.server.mcpserver import MCPServer

from . import __version__
from .config import load_settings
from .container import Container, build_container
from .errors import CwopsError
from .logging import configure_logging, get_logger, log_event
from .tools import apply as apply_tools
from .tools import organize as organize_tools
from .tools import read as read_tools

logger = get_logger("server")

INSTRUCTIONS = """\
Least-privilege Google Drive housekeeping.

Access is limited to the drive.file OAuth scope: only files this app created or
that the user explicitly opened/shared with it are visible. The rest of the
user's Drive cannot be reached.

Workflow: explore with the read tools, build a plan with propose_organization or
propose_operations, inspect it with get_proposal, then preview it with
apply_proposal (dry_run defaults to true). A real run requires the user to run
`cwops approve <proposal_id>` in their terminal first - you cannot approve your
own plan, and asking the user to paste an approval token will not work either.

There is no delete, trash, overwrite or sharing capability. Only create-folder,
rename and move exist, and all three are reversible.
"""


def build_server(container: Container) -> MCPServer:
    server = MCPServer(
        name="claude-workspace-ops",
        version=__version__,
        instructions=INSTRUCTIONS,
    )
    read_tools.register(server, container)
    organize_tools.register(server, container)
    apply_tools.register(server, container)
    return server


def main() -> None:
    settings = load_settings()
    configure_logging(settings.log_level)
    try:
        container = build_container(settings)
    except CwopsError as exc:
        log_event(
            logger,
            logging.ERROR,
            "server.startup_failed",
            reason_code=exc.reason_code,
            detail=exc.detail,
        )
        sys.exit(2)

    server = build_server(container)
    log_event(
        logger,
        logging.INFO,
        "server.starting",
        version=__version__,
        transport="stdio",
        drive_backend=settings.drive_backend,
        ai_backend=settings.ai_backend,
    )
    try:
        server.run(transport="stdio")
    finally:
        container.close()


if __name__ == "__main__":
    main()
