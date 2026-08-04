"""The `zeta events` command group."""

import json
from pathlib import Path

import click
from zeta.cli.common import runtime_event_store, state_dir_option
from zeta.cli.rendering import (
    descendant_events,
    event_record,
    print_event_item,
    print_event_sequence,
)
from zeta.events import DraftEvent
from zeta.harness.protocols import CancellationError
from zeta.journal.store import Filter


@click.group("events")
def events() -> None:
    """Inspect and publish durable runtime events."""


@click.group("waits")
def waits() -> None:
    """Inspect durable agent waits."""


@waits.command("list")
@state_dir_option
@click.option("--json", "json_output", is_flag=True, help="Emit JSON.")
def waits_list(
    state_dir: Path | None,
    json_output: bool,
) -> int:
    """List active and terminal waits."""

    event_store = runtime_event_store(state_dir)
    try:
        rows = event_store.list_waits()
    finally:
        event_store.close()
    if json_output:
        click.echo(json.dumps(rows, ensure_ascii=False))
        return 0
    if not rows:
        click.echo("waits empty")
        return 0
    for row in rows:
        deadline = row["deadline_ms"]
        click.echo(
            "\t".join(
                [
                    row["status"],
                    row["handle"],
                    row["agent_id"],
                    row["event_type"],
                    str(deadline) if deadline is not None else "-",
                ]
            )
        )
    return 0


@click.command("cancel")
@state_dir_option
@click.argument("handle")
@click.option("--reason", help="Why the future work is no longer needed.")
@click.option("--json", "json_output", is_flag=True, help="Emit JSON.")
def cancel(
    state_dir: Path | None,
    handle: str,
    reason: str | None,
    json_output: bool,
) -> int:
    """Cancel an active wait or pending scheduled event by HANDLE."""

    event_store = runtime_event_store(state_dir, read_only=False)
    try:
        result = event_store.cancel_resource(handle, reason=reason)
    except CancellationError as error:
        raise click.ClickException(str(error)) from error
    finally:
        event_store.close()
    if json_output:
        click.echo(
            json.dumps(
                {
                    "handle": result.handle,
                    "resource_type": result.resource_type,
                    "status": result.status,
                    "changed": result.changed,
                },
                ensure_ascii=False,
            )
        )
        return 0
    if result.changed:
        click.echo(f"cancelled {result.resource_type} {result.handle}")
    else:
        click.echo(f"{result.resource_type} {result.handle} is already {result.status}")
    return 0


@events.command("list")
@state_dir_option
@click.option("--type-prefix", help="Only show events with this type prefix.")
@click.option("--session", "session_id", help="Only show events for one session.")
@click.option(
    "--limit",
    type=click.IntRange(min=1),
    default=50,
    show_default=True,
    help="Maximum number of events to show.",
)
@click.option("--json", "json_output", is_flag=True, help="Emit JSON.")
def events_list(
    state_dir: Path | None,
    type_prefix: str | None,
    session_id: str | None,
    limit: int,
    json_output: bool,
) -> int:
    """List durable runtime events."""

    event_store = runtime_event_store(state_dir)
    try:
        durable_events = event_store.list_events(
            Filter(
                event_type_prefix=type_prefix,
                session_id=session_id,
                limit=limit,
            )
        )
    finally:
        event_store.close()
    if json_output:
        click.echo(json.dumps([event_record(event) for event in durable_events]))
        return 0
    if not durable_events:
        click.echo("events empty")
        return 0
    for event in durable_events:
        click.echo(
            "\t".join(
                [
                    str(event.cursor or ""),
                    event.event_type,
                    event.source,
                    event.id,
                ]
            )
        )
    return 0


@events.command("scheduled")
@state_dir_option
@click.option("--json", "json_output", is_flag=True, help="Emit JSON.")
def events_scheduled(
    state_dir: Path | None,
    json_output: bool,
) -> int:
    """List one-shot events requested by agents."""

    event_store = runtime_event_store(state_dir)
    try:
        scheduled_events = event_store.list_scheduled_events()
    finally:
        event_store.close()
    if json_output:
        click.echo(json.dumps(scheduled_events, ensure_ascii=False))
        return 0
    if not scheduled_events:
        click.echo("scheduled events empty")
        return 0
    for scheduled in scheduled_events:
        click.echo(
            "\t".join(
                [
                    scheduled["status"],
                    scheduled["handle"],
                    scheduled["event_type"],
                    str(scheduled["publish_at_ms"]),
                ]
            )
        )
    return 0


@events.command("cancel-scheduled")
@state_dir_option
@click.argument("handle")
def events_cancel_scheduled(
    state_dir: Path | None,
    handle: str,
) -> int:
    """Cancel one pending event request by HANDLE."""

    event_store = runtime_event_store(state_dir, read_only=False)
    try:
        status = event_store.cancel_scheduled_event(handle)
    finally:
        event_store.close()
    if status == "cancelled":
        click.echo(f"cancelled {handle}")
        return 0
    if status == "unknown":
        raise click.ClickException(f"scheduled event not found: {handle}")
    if status.startswith("already_"):
        terminal_status = status.removeprefix("already_")
        raise click.ClickException(
            f"scheduled event is already {terminal_status}: {handle}"
        )
    raise click.ClickException(f"scheduled event changed while cancelling: {handle}")


@events.command("publish")
@state_dir_option
@click.argument("event_type")
@click.option("--source", default="manual", show_default=True, help="Event source.")
@click.option(
    "--payload-json",
    default="{}",
    show_default=True,
    help="JSON object payload.",
)
@click.option("--idempotency-key", help="Optional idempotency key.")
@click.option("--caused-by", help="Optional parent event id.")
@click.option("--session", "session_id", help="Optional runtime session id.")
@click.option("--run-id", help="Optional runtime run id.")
@click.option("--turn-id", help="Optional runtime turn id.")
@click.option("--json", "json_output", is_flag=True, help="Emit JSON.")
def events_publish(
    state_dir: Path | None,
    event_type: str,
    source: str,
    payload_json: str,
    idempotency_key: str | None,
    caused_by: str | None,
    session_id: str | None,
    run_id: str | None,
    turn_id: str | None,
    json_output: bool,
) -> int:
    """Publish one durable event into the local runtime log."""
    if not event_type:
        raise click.ClickException("event_type must be non-empty")
    payload = event_payload_from_json(payload_json)
    event_store = runtime_event_store(state_dir, read_only=False)
    try:
        outcome = event_store.accept(
            DraftEvent(
                event_type,
                source,
                payload,
                idempotency_key=idempotency_key,
                caused_by=caused_by,
                session_id=session_id,
                run_id=run_id,
                turn_id=turn_id,
            )
        )
    finally:
        event_store.close()

    if json_output:
        click.echo(
            json.dumps(
                {"inserted": outcome.inserted, "event": event_record(outcome.event)},
                ensure_ascii=False,
            )
        )
        return 0
    status = "published" if outcome.inserted else "already published"
    click.echo(f"{status} {outcome.event.event_type} {outcome.event.id}")
    return 0


@events.command("chain")
@state_dir_option
@click.argument("event_id")
@click.option("--json", "json_output", is_flag=True, help="Emit JSON.")
@click.option("--raw", is_flag=True, help="With --json, return raw event payloads.")
def events_chain(
    state_dir: Path | None,
    event_id: str,
    json_output: bool,
    raw: bool,
) -> int:
    """Show the causal chain from root to EVENT_ID."""
    if raw and not json_output:
        raise click.UsageError("--raw requires --json")
    event_store = runtime_event_store(state_dir)
    try:
        chain = event_store.causal_chain(event_id)
    finally:
        event_store.close()
    return print_event_sequence(
        chain,
        json_output=json_output,
        raw=raw,
        empty_message=f"event not found: {event_id}",
    )


@events.command("root")
@state_dir_option
@click.argument("event_id")
@click.option("--json", "json_output", is_flag=True, help="Emit JSON.")
@click.option("--raw", is_flag=True, help="With --json, return raw event payload.")
def events_root(
    state_dir: Path | None,
    event_id: str,
    json_output: bool,
    raw: bool,
) -> int:
    """Show the root cause for EVENT_ID."""
    if raw and not json_output:
        raise click.UsageError("--raw requires --json")
    event_store = runtime_event_store(state_dir)
    try:
        chain = event_store.causal_chain(event_id)
    finally:
        event_store.close()
    return print_event_item(
        chain[0] if chain else None,
        json_output=json_output,
        raw=raw,
        empty_message=f"event not found: {event_id}",
    )


@events.command("descendants")
@state_dir_option
@click.argument("event_id")
@click.option("--json", "json_output", is_flag=True, help="Emit JSON.")
@click.option("--raw", is_flag=True, help="With --json, return raw event payloads.")
def events_descendants(
    state_dir: Path | None,
    event_id: str,
    json_output: bool,
    raw: bool,
) -> int:
    """Show events caused by EVENT_ID, recursively."""
    if raw and not json_output:
        raise click.UsageError("--raw requires --json")
    event_store = runtime_event_store(state_dir)
    try:
        descendants = descendant_events(event_store, event_id)
    finally:
        event_store.close()
    return print_event_sequence(
        descendants,
        json_output=json_output,
        raw=raw,
        empty_message=f"no descendants for event: {event_id}",
    )


@events.command("turn")
@state_dir_option
@click.argument("turn_id")
@click.option("--json", "json_output", is_flag=True, help="Emit JSON.")
@click.option("--raw", is_flag=True, help="With --json, return raw event payloads.")
def events_turn(
    state_dir: Path | None,
    turn_id: str,
    json_output: bool,
    raw: bool,
) -> int:
    """Show events associated with TURN_ID."""
    if raw and not json_output:
        raise click.UsageError("--raw requires --json")
    event_store = runtime_event_store(state_dir)
    try:
        turn_events = event_store.events_for_turn(turn_id)
    finally:
        event_store.close()
    return print_event_sequence(
        turn_events,
        json_output=json_output,
        raw=raw,
        empty_message=f"no events for turn: {turn_id}",
    )


def event_payload_from_json(payload_json: str) -> dict[str, object]:
    try:
        payload = json.loads(payload_json)
    except json.JSONDecodeError as exc:
        raise click.ClickException(f"invalid payload JSON: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise click.ClickException("payload JSON must be an object")
    return dict(payload)
