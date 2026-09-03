"""Shared tool plumbing: correlation IDs, audit rows, error translation.

Invariant I7 - every tool call writes exactly one audit row - lives here rather
than in each tool, so it cannot be forgotten in a new tool.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from ..container import Container
from ..errors import CwopsError
from ..logging import get_logger, log_event, new_correlation_id, set_correlation_id

logger = get_logger("tools")

#: Reason codes that mean "the system deliberately said no" rather than
#: "something broke". They are audited as ``refused``, not ``error``.
REFUSAL_CODES = frozenset(
    {
        "approval_required",
        "approval_invalid",
        "mutations_disabled",
        "plan_rejected",
        "outside_workspace",
        "not_granted",
        "limit_exceeded",
        "proposal_not_pending",
    }
)


@dataclass
class AuditContext:
    correlation_id: str
    proposal_id: str | None = None
    approved_by: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def note(self, **fields: Any) -> None:
        self.detail.update(fields)


@contextmanager
def audited(
    container: Container,
    tool: str,
    action: str,
    actor: str = "mcp_client",
) -> Iterator[AuditContext]:
    """Wrap one tool call: new correlation ID in, exactly one audit row out."""
    correlation_id = new_correlation_id()
    set_correlation_id(correlation_id)
    ctx = AuditContext(correlation_id=correlation_id)
    try:
        yield ctx
    except CwopsError as exc:
        outcome = "refused" if exc.reason_code in REFUSAL_CODES else "error"
        container.audit.record(
            actor=actor,  # type: ignore[arg-type]
            tool=tool,
            action=action,
            outcome=outcome,  # type: ignore[arg-type]
            proposal_id=ctx.proposal_id,
            reason_code=exc.reason_code,
            correlation_id=correlation_id,
            detail={**ctx.detail, **exc.context},
        )
        log_event(
            logger,
            logging.WARNING,
            f"tool.{outcome}",
            tool=tool,
            reason_code=exc.reason_code,
            detail=exc.detail,
        )
        # ToolError is the "failure you anticipated" channel: the client sees
        # the reason code instead of a generic crash message.
        raise ToolError(f"[{exc.reason_code}] {exc.detail}") from exc
    except Exception as exc:
        container.audit.record(
            actor=actor,  # type: ignore[arg-type]
            tool=tool,
            action=action,
            outcome="error",
            proposal_id=ctx.proposal_id,
            reason_code="internal_error",
            correlation_id=correlation_id,
            detail={**ctx.detail, "exception": type(exc).__name__},
        )
        log_event(logger, logging.ERROR, "tool.error", tool=tool, exception=type(exc).__name__)
        raise
    else:
        container.audit.record(
            actor=actor,  # type: ignore[arg-type]
            tool=tool,
            action=action,
            outcome="ok",
            proposal_id=ctx.proposal_id,
            approved_by=ctx.approved_by,
            correlation_id=correlation_id,
            detail=ctx.detail,
        )
        log_event(logger, logging.INFO, "tool.ok", tool=tool, **ctx.detail)
