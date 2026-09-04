"""Typed domain errors.

Every error carries a stable ``reason_code``. Reason codes are written to the
audit log and returned to the MCP client, so they are part of the public
contract and are asserted on in tests.
"""

from __future__ import annotations

from typing import Any


class CwopsError(Exception):
    """Base class for all domain errors."""

    reason_code = "internal_error"

    def __init__(self, detail: str, **context: Any) -> None:
        super().__init__(detail)
        self.detail = detail
        self.context: dict[str, Any] = context

    def to_dict(self) -> dict[str, Any]:
        return {
            "reason_code": self.reason_code,
            "detail": self.detail,
            "context": self.context,
        }


class ConfigurationError(CwopsError):
    reason_code = "configuration_error"


class MutationsDisabled(CwopsError):
    """CWOPS_ALLOW_MUTATIONS is false. Gate 1 of 3."""

    reason_code = "mutations_disabled"


class ApprovalRequired(CwopsError):
    """No human approval record exists for this proposal. Gate 2 of 3."""

    reason_code = "approval_required"


class ApprovalNotInteractive(CwopsError):
    """`cwops approve` was invoked without a human at a terminal.

    Approval is only meaningful if a person is present to give it, so a
    non-interactive invocation - a spawned subprocess, a pipe, a CI step, an
    agent shell - is refused rather than attributed to the OS user.
    """

    reason_code = "approval_not_interactive"


class ApprovalInvalid(CwopsError):
    """An approval exists but is expired, consumed, or bound to a different plan."""

    reason_code = "approval_invalid"


class PlanRejected(CwopsError):
    """Deterministic validation refused one or more operations."""

    reason_code = "plan_rejected"


class OutsideWorkspace(CwopsError):
    """An operation targets something outside the app-owned root folder."""

    reason_code = "outside_workspace"


class NotGranted(CwopsError):
    """The server has no verified drive.file grant for this file ID."""

    reason_code = "not_granted"


class ProposalNotFound(CwopsError):
    reason_code = "proposal_not_found"


class ProposalNotPending(CwopsError):
    reason_code = "proposal_not_pending"


class LimitExceeded(CwopsError):
    reason_code = "limit_exceeded"


class DriveApiError(CwopsError):
    reason_code = "drive_api_error"


class AiResponseError(CwopsError):
    """Claude returned something that could not be parsed or validated."""

    reason_code = "ai_response_error"
