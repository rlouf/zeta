"""Authored-agent event vocabulary."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError


class EventRegistryError(ValueError):
    """Raised when an event registry entry is invalid."""


class EventRegistry:
    """Known event types and optional payload schemas."""

    def __init__(
        self,
        events: Mapping[str, Mapping[str, Any] | None] | None = None,
    ) -> None:
        self._schemas: dict[str, dict[str, Any] | None] = {}
        for event_type, schema in (events or {}).items():
            self.register(event_type, schema)

    def register(
        self,
        event_type: str,
        schema: Mapping[str, Any] | None = None,
    ) -> None:
        if event_type in self._schemas:
            raise EventRegistryError(f"event {event_type!r} is already registered")
        parsed_schema = dict(schema) if schema is not None else None
        if parsed_schema is not None:
            try:
                Draft202012Validator.check_schema(parsed_schema)
            except SchemaError as exc:
                raise EventRegistryError(
                    f"event {event_type!r} has a malformed schema: {exc.message}"
                ) from exc
        self._schemas[event_type] = parsed_schema

    def knows(self, event_type: str) -> bool:
        return event_type in self._schemas

    def schema(self, event_type: str) -> dict[str, Any] | None:
        schema = self._schemas.get(event_type)
        return dict(schema) if schema is not None else None
