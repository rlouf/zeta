"""Read-only runtime tools for session history and active context state."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from zeta.capabilities.executors import CapabilityFunction
from zeta.capabilities.types import Capability, CapabilityId
from zeta.context.builder import prompt_builder_params
from zeta.context.transforms import BudgetThresholdPromptTransform
from zeta.models.chat_completions import DEFAULT_MAX_COMPLETION_TOKENS
from zeta.models.limits import model_context_tokens
from zeta.substrate import ObjectId, Store
from zeta.trace.query import QueryLogReader
from zeta.trace.summarize import estimated_prompt_tokens

QUERY_LOG_CAPABILITY_ID = "zeta.query_log"
QUERY_CONTEXT_BUDGET_CAPABILITY_ID = "zeta.query_context_budget"

QUERY_LOG_SPEC = Capability(
    CapabilityId("zeta", "query_log"),
    (
        "Query prior model runs in the current authorized session. Use it to "
        "recover earlier decisions, outcomes, tool activity, and prompt trace ids. "
        "Cite the returned run ids when relying on history."
    ),
    {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "since": {
                "type": "string",
                "description": (
                    "Only runs at or after YYYY-MM-DD, or an age like 2d, 6h, or 30m."
                ),
            },
            "failed": {
                "type": "boolean",
                "description": "Only failed or aborted runs.",
            },
            "run_id": {
                "type": "string",
                "description": "Expand one prior run by full id or unique id prefix.",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 50,
                "description": "Maximum number of prior runs to list.",
            },
        },
    },
)

QUERY_CONTEXT_BUDGET_SPEC = Capability(
    CapabilityId("zeta", "query_context_budget"),
    (
        "Report the active model context budget, latest prompt use, output "
        "reservation, remaining tokens, and prompt compaction settings."
    ),
    {
        "type": "object",
        "additionalProperties": False,
        "properties": {},
    },
)


@dataclass(frozen=True)
class ContextBudgetBinding:
    """Expose only the active run values needed to calculate its context budget."""

    telemetry: Mapping[str, Any]
    prompt_object_id: ObjectId | None = None
    store: Store | None = None
    selected_url: str | None = None
    selected_model: str | None = None
    compaction_strategy: str = "off"
    compaction_threshold_tokens: int | None = None


def bind_history_tools(
    reader: QueryLogReader | None,
) -> dict[str, CapabilityFunction]:
    """Bind the authorized reader so history cannot cross a session boundary."""
    return {
        QUERY_LOG_CAPABILITY_ID: lambda params: query_log(params, reader=reader),
    }


def bind_context_budget_tools(
    binding: ContextBudgetBinding,
) -> dict[str, CapabilityFunction]:
    """Bind context observation to values from the active model turn."""
    return {
        QUERY_CONTEXT_BUDGET_CAPABILITY_ID: lambda params: query_context_budget(
            params,
            binding=binding,
        ),
    }


def context_compaction_settings(transform: object) -> tuple[str, int | None]:
    """Report the effective threshold without exposing prompt transform objects."""
    if not isinstance(transform, BudgetThresholdPromptTransform):
        return "off", None
    strategies = {
        "PromptStructuralTrim:v1": "structural_trim",
        "PromptDropOldest:v1": "drop_oldest",
        "PromptTaskStateExtractor:v1": "task_state",
    }
    return strategies.get(transform.producer, "custom"), transform.max_tokens


def query_log(
    params: dict[str, Any],
    *,
    reader: QueryLogReader | None,
) -> dict[str, Any]:
    if reader is None:
        return query_log_unavailable(params)
    return reader(params)


def query_log_unavailable(_params: dict[str, Any]) -> dict[str, Any]:
    """Refuse history access when no durable session authorizes a reader."""
    return {
        "ok": False,
        "error": {
            "code": "query-log-unavailable",
            "message": "query_log is unavailable outside a durable runtime session",
        },
    }


def query_context_budget(
    _params: dict[str, Any],
    *,
    binding: ContextBudgetBinding,
) -> dict[str, Any]:
    prompt_tokens, prompt_tokens_source = _prompt_tokens(binding)
    context_window_tokens = _context_window_tokens(binding)
    reserved_output_tokens = _reserved_output_tokens(binding)
    remaining_tokens = _remaining_tokens(
        context_window_tokens,
        prompt_tokens,
        reserved_output_tokens,
    )
    return {
        "ok": True,
        "context_window_tokens": context_window_tokens,
        "prompt_tokens": prompt_tokens,
        "prompt_tokens_source": prompt_tokens_source,
        "reserved_output_tokens": reserved_output_tokens,
        "remaining_tokens": remaining_tokens,
        "usage_ratio": _usage_ratio(
            context_window_tokens,
            prompt_tokens,
            reserved_output_tokens,
        ),
        "compaction_strategy": binding.compaction_strategy,
        "compaction_threshold_tokens": binding.compaction_threshold_tokens,
    }


def query_context_budget_unavailable(_params: dict[str, Any]) -> dict[str, Any]:
    """Refuse a context query when no active run can supply its state."""
    return {
        "ok": False,
        "error": {
            "code": "query-context-budget-unavailable",
            "message": "query_context_budget is unavailable outside an active run",
        },
    }


def _prompt_tokens(binding: ContextBudgetBinding) -> tuple[int | None, str]:
    measured = _provider_prompt_tokens(binding.telemetry)
    if measured is not None:
        return measured, "provider"
    estimated = _stored_prompt_tokens(binding)
    if estimated is not None:
        return estimated, "estimate"
    return None, "unavailable"


def _provider_prompt_tokens(telemetry: Mapping[str, Any]) -> int | None:
    usage = telemetry.get("usage")
    values = usage if isinstance(usage, Mapping) else telemetry
    for key in ("prompt_tokens", "input_tokens"):
        tokens = _non_negative_int(values.get(key))
        if tokens is not None:
            return tokens
    return None


def _stored_prompt_tokens(binding: ContextBudgetBinding) -> int | None:
    if binding.store is None or binding.prompt_object_id is None:
        return None
    prompt = binding.store.get_object(binding.prompt_object_id)
    if prompt is None or prompt.kind != "prompt":
        return None
    return estimated_prompt_tokens(prompt.links, binding.store.get_object)


def _context_window_tokens(binding: ContextBudgetBinding) -> int | None:
    telemetry_tokens = _positive_int(binding.telemetry.get("model_context_tokens"))
    if telemetry_tokens is not None:
        return telemetry_tokens
    return model_context_tokens(binding.selected_url, binding.selected_model)


def _reserved_output_tokens(binding: ContextBudgetBinding) -> int:
    if binding.store is None or binding.prompt_object_id is None:
        return DEFAULT_MAX_COMPLETION_TOKENS
    max_tokens, _selected_model, _thinking = prompt_builder_params(
        binding.store,
        binding.prompt_object_id,
    )
    return max_tokens


def _remaining_tokens(
    context_window_tokens: int | None,
    prompt_tokens: int | None,
    reserved_output_tokens: int,
) -> int | None:
    if context_window_tokens is None or prompt_tokens is None:
        return None
    return context_window_tokens - prompt_tokens - reserved_output_tokens


def _usage_ratio(
    context_window_tokens: int | None,
    prompt_tokens: int | None,
    reserved_output_tokens: int,
) -> float | None:
    if context_window_tokens is None or prompt_tokens is None:
        return None
    usable_prompt_tokens = context_window_tokens - reserved_output_tokens
    if usable_prompt_tokens <= 0:
        return None
    return prompt_tokens / usable_prompt_tokens


def _non_negative_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _positive_int(value: Any) -> int | None:
    parsed = _non_negative_int(value)
    if parsed is None or parsed == 0:
        return None
    return parsed
