"""Trace inspection helpers for the Zeta CLI."""

import logging

LOGGER = logging.getLogger("zeta.trace")
_WARNED_FAILURES: set[str] = set()


def warn_trace_failure_once(operation: str, exc: BaseException) -> None:
    """Log one warning per trace operation before fail-open degradation."""
    if operation in _WARNED_FAILURES:
        return
    _WARNED_FAILURES.add(operation)
    LOGGER.warning("trace disabled for %s after failure: %s", operation, exc)
