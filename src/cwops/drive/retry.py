"""Retry policy for Drive calls.

Only transient failures are retried. A 404 under the drive.file scope means
"you were not granted this file" - retrying it is pointless and would turn a
clear authorization signal into a slow timeout.

``sleep`` is injectable so the tests exercise the real backoff logic instantly.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any, TypeVar

from tenacity import (
    RetryCallState,
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

from ..errors import DriveApiError
from ..logging import get_logger, log_event

logger = get_logger("drive.retry")

T = TypeVar("T")

#: Transient upstream conditions. 403 is deliberately absent: from Drive it
#: usually means quota or permission, and blindly retrying a quota error makes
#: the situation worse.
RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})

DEFAULT_ATTEMPTS = 4
INITIAL_DELAY = 0.5
MAX_DELAY = 8.0


def is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, DriveApiError):
        status = exc.context.get("status")
        return isinstance(status, int) and status in RETRYABLE_STATUS
    return isinstance(exc, (ConnectionError, TimeoutError))


def _log_retry(state: RetryCallState) -> None:
    exc = state.outcome.exception() if state.outcome else None
    log_event(
        logger,
        logging.WARNING,
        "drive.retry",
        attempt=state.attempt_number,
        sleeping=round(state.idle_for, 3),
        error=str(exc)[:200] if exc else None,
    )


def call_with_retry(
    operation: Callable[[], T],
    *,
    attempts: int = DEFAULT_ATTEMPTS,
    sleep: Callable[[float], Any] = time.sleep,
) -> T:
    """Run ``operation``, retrying transient failures with jittered backoff."""
    retryer = Retrying(
        retry=retry_if_exception(is_retryable),
        stop=stop_after_attempt(attempts),
        wait=wait_exponential_jitter(initial=INITIAL_DELAY, max=MAX_DELAY),
        before_sleep=_log_retry,
        sleep=sleep,
        reraise=True,
    )
    return retryer(operation)
