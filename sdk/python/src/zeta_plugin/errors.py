"""Public errors for Python provider implementations."""

from __future__ import annotations

import re

_CODE = re.compile(r"^[a-z][a-z0-9_]*$")


class ProviderError(Exception):
    """Reports one provider failure with a stable retry contract."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "provider_error",
        retryable: bool = False,
    ) -> None:
        if not message:
            raise ValueError("A provider error message must not be empty")
        if not _CODE.fullmatch(code):
            raise ValueError("A provider error code must use lower-case snake case")
        super().__init__(message)
        self.message = message
        self.code = code
        self.retryable = retryable
