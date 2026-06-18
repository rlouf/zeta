"""Substrate refs and ref errors."""

from __future__ import annotations

from .object import ObjectId

REF_EXPECTED_UNSET = object()


class UnknownIdError(LookupError):
    """A trace id token matched no ref, object id, or prefix."""

    def __init__(self, token: str) -> None:
        super().__init__(token)
        self.token = token


class AmbiguousIdError(LookupError):
    """A trace id prefix matched more than one object."""

    def __init__(self, token: str, candidates: list[ObjectId]) -> None:
        super().__init__(token)
        self.token = token
        self.candidates = candidates


class UnknownSessionError(LookupError):
    """A session id named no recorded trace store."""

    def __init__(self, session_id: str, available: list[str]) -> None:
        super().__init__(session_id)
        self.session_id = session_id
        self.available = available


class RefConflictError(RuntimeError):
    """A mutable ref did not match the caller's observed value."""

    def __init__(
        self,
        name: str,
        *,
        expected: ObjectId | None,
        actual: ObjectId | None,
    ) -> None:
        super().__init__(
            f"ref {name!r} changed: expected {expected!r}, found {actual!r}"
        )
        self.name = name
        self.expected = expected
        self.actual = actual
