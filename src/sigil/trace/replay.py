"""Trace replay helpers."""

import difflib
from typing import Any

import click

from sigil.display.summarize import (
    assistant_trace_message,
    assistant_trace_summary,
    short_trace_id,
)
from zeta.models import ModelSelection, resolve_active_model, resolve_model_profile
from zeta.models.types import ModelOutput
from zeta.records.objects import Derivation, Object, ObjectId
from zeta.records.stores import Store, warn_trace_failure_once


def replay_model_selection(model_profile: str | None) -> ModelSelection:
    """Return the model a replay should use, honoring --model."""
    if model_profile is None:
        from sigil.sessions import session_dir

        return resolve_active_model(session_dir=session_dir()).selection
    selection = resolve_model_profile(model_profile)
    if selection is None:
        raise click.ClickException(f"unknown model profile: {model_profile}")
    return selection


def latest_model_answer(
    store: Store,
    prompt_id: ObjectId,
) -> tuple[ObjectId, str] | None:
    """Return the newest recorded assistant answer for a prompt."""
    answer_ids = [
        derivation.output_id
        for derivation in store.derivations_for_input(prompt_id)
        if derivation.producer == "ModelResponse"
    ]
    for answer_id in reversed(answer_ids):
        obj = store.get_object(answer_id)
        if obj is None:
            continue
        message = assistant_trace_message(obj.data)
        if message is not None:
            return answer_id, answer_display_text(message)
    return None


def answer_display_text(message: dict[str, Any]) -> str:
    """Return an assistant message's text, or its tool calls when text-free."""
    content = str(message.get("content") or "")
    if content:
        return content
    return assistant_trace_summary({"message": message})


def record_replay(
    store: Store,
    prompt_id: ObjectId,
    message: dict[str, Any],
    selection: ModelSelection,
) -> ObjectId | None:
    """Record the replay answer in the trace graph, fail-open."""
    try:
        with store.batch():
            replay_id = store.put_object(
                Object(
                    kind="assistant_message",
                    schema="zeta.model_output.v1",
                    data=model_output_trace_data(ModelOutput(message=message)),
                    links=(prompt_id,),
                )
            )
            store.record_derivation(
                Derivation(
                    producer="ModelReplay",
                    output_id=replay_id,
                    input_ids=(prompt_id,),
                    params={"profile": selection.profile, "model": selection.model},
                )
            )
        return replay_id
    except Exception as exc:
        warn_trace_failure_once("trace_replay", exc)
        return None


def model_output_trace_data(output: ModelOutput) -> dict[str, Any]:
    model_output: dict[str, Any] = {"message": dict(output.message)}
    if output.finish_reason is not None:
        model_output["finish_reason"] = output.finish_reason
    if output.usage is not None:
        usage: dict[str, int] = {}
        if output.usage.prompt_tokens is not None:
            usage["prompt_tokens"] = output.usage.prompt_tokens
        if output.usage.completion_tokens is not None:
            usage["completion_tokens"] = output.usage.completion_tokens
        if output.usage.total_tokens is not None:
            usage["total_tokens"] = output.usage.total_tokens
        model_output["usage"] = usage
    if output.provider_metadata:
        model_output["provider_metadata"] = dict(output.provider_metadata)
    if output.provider_replay_items:
        model_output["provider_replay_items"] = [
            dict(item) for item in output.provider_replay_items
        ]
    return {
        "message": dict(output.message),
        "model_output": model_output,
    }


def render_replay(
    prompt_id: ObjectId,
    payload_verified: bool,
    selection: ModelSelection,
    original: tuple[ObjectId, str] | None,
    replay_id: ObjectId | None,
    replay_content: str,
    *,
    diff_output: bool,
) -> list[str]:
    """Render the replay outcome as plain forensic lines."""
    verification = "verified" if payload_verified else "differs from the recorded hash"
    lines = [
        f"prompt   {short_trace_id(prompt_id)}  payload {verification}",
        f"model    {selection.profile} -> {selection.model} @ {selection.url}",
        "",
    ]
    original_label = short_trace_id(original[0]) if original else "(none recorded)"
    original_content = original[1] if original else ""
    replay_label = short_trace_id(replay_id) if replay_id else "(unrecorded)"
    if diff_output:
        lines.extend(
            difflib.unified_diff(
                original_content.splitlines(),
                replay_content.splitlines(),
                fromfile=f"original {original_label}",
                tofile=f"replay {replay_label}",
                lineterm="",
            )
        )
        return lines
    lines.append(f"original {original_label}")
    if original_content:
        lines.append(original_content)
    lines.extend(["", f"replay   {replay_label}"])
    if replay_content:
        lines.append(replay_content)
    return lines
