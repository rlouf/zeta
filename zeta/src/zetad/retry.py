"""Retry policy and structured dispatch failure classification."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from zeta.agents.spec import SpecError

__all__ = [
    "DispatchErrorCode",
    "FailureClass",
    "RetryPolicy",
    "classify_error_code",
    "error_code_for_exception",
]

DispatchErrorCode = Literal[
    "agent_spec_invalid",
    "malformed_event_payload",
    "provider_timeout",
    "network_error",
    "tool_failed",
    "agent_execution_failed",
]

FailureClass = Literal["retryable", "permanent"]

PERMANENT_ERROR_CODES: frozenset[str] = frozenset(
    {
        "agent_spec_invalid",
        "malformed_event_payload",
    }
)


@dataclass(frozen=True)
class RetryPolicy:
    """Bounded exponential backoff policy for queue item retries."""

    max_attempts: int = 3
    backoff_base_seconds: float = 5.0
    backoff_factor: float = 2.0
    backoff_max_seconds: float = 300.0

    def __post_init__(self) -> None:
        if (
            not isinstance(self.max_attempts, int)
            or isinstance(self.max_attempts, bool)
            or self.max_attempts < 1
        ):
            raise ValueError("max_attempts must be a positive integer")
        if not isinstance(self.backoff_base_seconds, int | float) or isinstance(
            self.backoff_base_seconds, bool
        ):
            raise ValueError("backoff_base_seconds must be a number")
        if self.backoff_base_seconds < 0:
            raise ValueError("backoff_base_seconds must be non-negative")
        if not isinstance(self.backoff_factor, int | float) or isinstance(
            self.backoff_factor, bool
        ):
            raise ValueError("backoff_factor must be a number")
        if self.backoff_factor < 1:
            raise ValueError("backoff_factor must be at least 1")
        if not isinstance(self.backoff_max_seconds, int | float) or isinstance(
            self.backoff_max_seconds, bool
        ):
            raise ValueError("backoff_max_seconds must be a number")
        if self.backoff_max_seconds < 0:
            raise ValueError("backoff_max_seconds must be non-negative")

    def delay_seconds(self, attempt_number: int) -> float:
        """Return the retry delay after the given failed attempt number."""

        if attempt_number < 1:
            raise ValueError("attempt_number must be positive")
        delay = self.backoff_base_seconds * (
            self.backoff_factor ** (attempt_number - 1)
        )
        return min(delay, self.backoff_max_seconds)

    def delay_ms(self, attempt_number: int) -> int:
        """Return the retry delay in whole milliseconds."""

        return int(self.delay_seconds(attempt_number) * 1000)

    def deterministic_jitter_seconds(self, key: str, *, spread_seconds: float) -> float:
        """Return stable jitter in [0, spread_seconds] for one queue item key."""

        if spread_seconds <= 0:
            return 0.0
        bucket = sum(key.encode("utf-8")) % 10_000
        return spread_seconds * (bucket / 10_000)

    def classify(self, error_code: str) -> FailureClass:
        """Classify a structured dispatch error code."""

        return classify_error_code(error_code)


def classify_error_code(error_code: str) -> FailureClass:
    """Return whether a structured dispatch error is retryable."""

    if error_code in PERMANENT_ERROR_CODES:
        return "permanent"
    return "retryable"


def error_code_for_exception(exc: Exception) -> DispatchErrorCode:
    """Map known exception classes to stable dispatch error codes."""

    if isinstance(exc, SpecError):
        return "agent_spec_invalid"
    if isinstance(exc, TimeoutError):
        return "provider_timeout"
    return "agent_execution_failed"
