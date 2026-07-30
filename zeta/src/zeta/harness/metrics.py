"""Small, backend-neutral runtime metrics boundary."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from threading import Lock
from typing import Protocol, TypeAlias

MetricAttribute: TypeAlias = str | int | float | bool


class RuntimeMetrics(Protocol):
    """Receives runtime measurements without owning their export policy."""

    def observe(
        self,
        name: str,
        value: float,
        *,
        attributes: Mapping[str, MetricAttribute] | None = None,
    ) -> None: ...


class NullRuntimeMetrics:
    """Default sink used when no metrics backend is configured."""

    def observe(
        self,
        name: str,
        value: float,
        *,
        attributes: Mapping[str, MetricAttribute] | None = None,
    ) -> None:
        del name, value, attributes


@dataclass(frozen=True)
class MetricSample:
    """One measurement captured by an in-memory sink."""

    name: str
    value: float
    attributes: Mapping[str, MetricAttribute]


@dataclass
class InMemoryRuntimeMetrics:
    """Thread-safe metrics sink for tests and embedded runtimes."""

    samples: list[MetricSample] = field(default_factory=list)
    _lock: Lock = field(default_factory=Lock, init=False, repr=False)

    def observe(
        self,
        name: str,
        value: float,
        *,
        attributes: Mapping[str, MetricAttribute] | None = None,
    ) -> None:
        sample = MetricSample(name, float(value), dict(attributes or {}))
        with self._lock:
            self.samples.append(sample)
