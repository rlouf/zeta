"""The `zeta agents` command group."""

import json
from pathlib import Path
from typing import Any

import click
from zeta.capabilities.registry import (
    AgentToolDefinition,
    AgentToolDefinitionError,
    agent_tool_definition_from_content,
)
from zeta.cli.common import runtime_event_store, state_dir_option
from zeta.context.transforms import (
    ContentConflict,
    ContentHead,
    ContentRevision,
    ContentValidationError,
    advance_content_head,
    content_head_history,
    content_node_from_object,
    content_revision_from_object,
    restore_content_head,
)
from zeta.substrate import AmbiguousIdError, Store, UnknownIdError, resolve_object_id


@click.group("agents")
def agents() -> None:
    """Manage authored agents."""


@agents.group("content")
def agents_content() -> None:
    """Inspect and restore durable agent content."""


@agents.group("tools")
def agents_tools() -> None:
    """Inspect and restore graph-authored agent tools."""


@agents_tools.command("list")
@click.argument("agent")
@state_dir_option
@click.option("--json", "json_output", is_flag=True, help="Emit JSON.")
def agents_tools_list(
    agent: str,
    state_dir: Path | None,
    json_output: bool,
) -> int:
    """List active graph-authored tools for AGENT."""

    runtime = runtime_event_store(state_dir)
    try:
        store = runtime.content_store()
        _head_id, revision = _active_agent_revision(store, agent)
        result = _agent_tool_list(store, agent, revision)
    except (AgentToolDefinitionError, ContentValidationError, RuntimeError) as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        runtime.close()
    if json_output:
        click.echo(json.dumps(result, ensure_ascii=False))
    elif not result:
        click.echo("tools empty")
    else:
        for item in result:
            click.echo(f"{item['name']}\t{item['capability_id']}\t{item['object_id']}")
    return 0


@agents_tools.command("show")
@click.argument("agent")
@click.argument("tool")
@state_dir_option
@click.option("--json", "json_output", is_flag=True, help="Emit JSON.")
def agents_tools_show(
    agent: str,
    tool: str,
    state_dir: Path | None,
    json_output: bool,
) -> int:
    """Show one active graph-authored TOOL for AGENT."""

    runtime = runtime_event_store(state_dir)
    try:
        store = runtime.content_store()
        _head_id, revision = _active_agent_revision(store, agent)
        definition = _active_agent_tool(store, agent, tool, revision)
        result = _agent_tool_view(definition)
    except (
        AgentToolDefinitionError,
        ContentValidationError,
        RuntimeError,
    ) as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        runtime.close()
    if json_output:
        click.echo(json.dumps(result, ensure_ascii=False))
    else:
        click.echo(f"{definition.capability_id}\t{definition.object_id}")
        click.echo(definition.source)
    return 0


@agents_tools.command("disable")
@click.argument("agent")
@click.argument("tool")
@state_dir_option
@click.option("--reason", required=True, help="Explain why this tool is disabled.")
@click.option("--json", "json_output", is_flag=True, help="Emit JSON.")
def agents_tools_disable(
    agent: str,
    tool: str,
    state_dir: Path | None,
    reason: str,
    json_output: bool,
) -> int:
    """Disable one graph-authored TOOL in a new agent revision."""

    runtime = runtime_event_store(state_dir, read_only=False)
    try:
        store = runtime.content_store()
        current_id, revision = _active_agent_revision(store, agent)
        definition = _active_agent_tool(store, agent, tool, revision)
        nodes = dict(revision.nodes)
        nodes.pop(definition.key)
        source_scopes = dict(revision.source_scopes)
        source_scopes.pop(definition.key)
        head = advance_content_head(
            store,
            ContentHead("agent", agent, agent),
            expected_head=current_id,
            nodes=nodes,
            projection_order=tuple(
                key for key in revision.projection_order if key != definition.key
            ),
            source_scopes=source_scopes,
            reason=reason,
            source_ids=(definition.object_id,),
        )
        result = {
            "agent": agent,
            "tool": definition.name,
            "old_head": current_id,
            "head": head,
            "disabled_object_id": definition.object_id,
            "reason": reason,
        }
    except (
        AgentToolDefinitionError,
        ContentConflict,
        ContentValidationError,
        RuntimeError,
    ) as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        runtime.close()
    if json_output:
        click.echo(json.dumps(result, ensure_ascii=False))
    else:
        click.echo(f"disabled {definition.capability_id} in {head}")
    return 0


@agents_tools.command("restore")
@click.argument("agent")
@click.argument("tool")
@click.argument("version")
@state_dir_option
@click.option("--reason", required=True, help="Explain why this version is restored.")
@click.option("--json", "json_output", is_flag=True, help="Emit JSON.")
def agents_tools_restore(
    agent: str,
    tool: str,
    version: str,
    state_dir: Path | None,
    reason: str,
    json_output: bool,
) -> int:
    """Restore an earlier graph-authored TOOL version for AGENT."""

    runtime = runtime_event_store(state_dir, read_only=False)
    try:
        store = runtime.content_store()
        current_id, revision = _active_agent_revision(store, agent)
        definition = _resolve_agent_tool_version(store, agent, tool, version)
        if not _tool_version_in_history(
            store,
            current_id,
            definition.key,
            definition.object_id,
        ):
            raise ContentValidationError(
                "tool version is not in the active agent content history"
            )
        nodes = dict(revision.nodes)
        is_new_key = definition.key not in nodes
        nodes[definition.key] = definition.object_id
        source_scopes = dict(revision.source_scopes)
        source_scopes[definition.key] = "agent"
        head = advance_content_head(
            store,
            ContentHead("agent", agent, agent),
            expected_head=current_id,
            nodes=nodes,
            projection_order=(
                (*revision.projection_order, definition.key)
                if is_new_key
                else revision.projection_order
            ),
            source_scopes=source_scopes,
            reason=reason,
            source_ids=(definition.object_id,),
        )
        result = {
            "agent": agent,
            "tool": definition.name,
            "old_head": current_id,
            "head": head,
            "object_id": definition.object_id,
            "reason": reason,
        }
    except (
        AgentToolDefinitionError,
        AmbiguousIdError,
        ContentConflict,
        ContentValidationError,
        RuntimeError,
        UnknownIdError,
    ) as exc:
        raise click.ClickException(_content_error(exc)) from exc
    finally:
        runtime.close()
    if json_output:
        click.echo(json.dumps(result, ensure_ascii=False))
    else:
        click.echo(f"restored {definition.capability_id} in {head}")
    return 0


@agents_content.command("show")
@click.argument("agent")
@state_dir_option
@click.option("--json", "json_output", is_flag=True, help="Emit JSON.")
def agents_content_show(
    agent: str,
    state_dir: Path | None,
    json_output: bool,
) -> int:
    """Show the active content revision for AGENT."""

    runtime = runtime_event_store(state_dir)
    try:
        store = runtime.content_store()
        head_id, revision = _active_agent_revision(store, agent)
        result = _agent_content_view(store, agent, head_id, revision)
    except (ContentValidationError, RuntimeError) as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        runtime.close()
    if json_output:
        click.echo(json.dumps(result, ensure_ascii=False))
    else:
        click.echo(f"{agent}\t{head_id}")
        for item in result["nodes"]:
            click.echo(f"{item['kind']}\t{item['key']}\t{item['object_id']}")
    return 0


@agents_content.command("log")
@click.argument("agent")
@state_dir_option
@click.option("--limit", type=click.IntRange(1, 1000), default=100, show_default=True)
@click.option("--json", "json_output", is_flag=True, help="Emit JSON.")
def agents_content_log(
    agent: str,
    state_dir: Path | None,
    limit: int,
    json_output: bool,
) -> int:
    """List active and prior content revisions for AGENT."""

    runtime = runtime_event_store(state_dir)
    try:
        store = runtime.content_store()
        head_id, _revision = _active_agent_revision(store, agent)
        result = [
            _agent_content_log_item(store, item)
            for item in content_head_history(store, head_id, limit=limit)
        ]
    except (ContentValidationError, RuntimeError) as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        runtime.close()
    if json_output:
        click.echo(json.dumps(result, ensure_ascii=False))
    else:
        for item in result:
            click.echo(f"{item['head']}\t{item['reason']}")
    return 0


@agents_content.command("diff")
@click.argument("agent")
@click.argument("old_head")
@click.argument("new_head")
@state_dir_option
@click.option("--json", "json_output", is_flag=True, help="Emit JSON.")
def agents_content_diff(
    agent: str,
    old_head: str,
    new_head: str,
    state_dir: Path | None,
    json_output: bool,
) -> int:
    """Compare two content revisions for AGENT."""

    runtime = runtime_event_store(state_dir)
    try:
        store = runtime.content_store()
        old_id, old_revision = _resolve_agent_revision(store, agent, old_head)
        new_id, new_revision = _resolve_agent_revision(store, agent, new_head)
        result = _agent_content_diff(agent, old_id, old_revision, new_id, new_revision)
    except (
        AmbiguousIdError,
        ContentValidationError,
        RuntimeError,
        UnknownIdError,
    ) as exc:
        raise click.ClickException(_content_error(exc)) from exc
    finally:
        runtime.close()
    if json_output:
        click.echo(json.dumps(result, ensure_ascii=False))
    else:
        for item in result["added"]:
            click.echo(f"+\t{item['key']}\t{item['object_id']}")
        for item in result["removed"]:
            click.echo(f"-\t{item['key']}\t{item['object_id']}")
        for item in result["changed"]:
            click.echo(
                f"~\t{item['key']}\t{item['old_object_id']}\t{item['new_object_id']}"
            )
    return 0


@agents_content.command("restore")
@click.argument("agent")
@click.argument("head")
@state_dir_option
@click.option("--reason", required=True, help="Explain why this revision is restored.")
@click.option("--json", "json_output", is_flag=True, help="Emit JSON.")
def agents_content_restore(
    agent: str,
    head: str,
    state_dir: Path | None,
    reason: str,
    json_output: bool,
) -> int:
    """Move AGENT's content ref to an earlier revision."""

    runtime = runtime_event_store(state_dir, read_only=False)
    try:
        store = runtime.content_store()
        current_id, _current = _active_agent_revision(store, agent)
        target_id, _target = _resolve_agent_revision(store, agent, head)
        if target_id not in content_head_history(store, current_id, limit=1000):
            raise ContentValidationError(
                "target revision is not in the active agent content history"
            )
        restored = restore_content_head(
            store,
            ContentHead("agent", agent, agent),
            expected_head=current_id,
            target_head=target_id,
            reason=reason,
        )
        result = {
            "agent": agent,
            "old_head": current_id,
            "head": restored,
            "reason": reason,
        }
    except (
        AmbiguousIdError,
        ContentConflict,
        ContentValidationError,
        RuntimeError,
        UnknownIdError,
    ) as exc:
        raise click.ClickException(_content_error(exc)) from exc
    finally:
        runtime.close()
    if json_output:
        click.echo(json.dumps(result, ensure_ascii=False))
    else:
        click.echo(f"restored {agent} from {current_id} to {restored}")
    return 0


def _active_agent_revision(
    store: Store,
    agent: str,
) -> tuple[str, ContentRevision]:
    ref = store.get_ref(f"agent/{agent}/content/head")
    if ref is None:
        raise ContentValidationError(f"agent {agent!r} has no durable content")
    return _resolve_agent_revision(store, agent, ref.object_id)


def _resolve_agent_revision(
    store: Store,
    agent: str,
    token: str,
) -> tuple[str, ContentRevision]:
    object_id = resolve_object_id(store, token)
    revision = content_revision_from_object(store.get_object(object_id))
    if revision.owner != agent:
        raise ContentValidationError(
            f"content revision belongs to {revision.owner!r}, not {agent!r}"
        )
    return object_id, revision


def _agent_content_view(
    store: Store,
    agent: str,
    head_id: str,
    revision: ContentRevision,
) -> dict[str, Any]:
    nodes = []
    for key in revision.projection_order:
        object_id = revision.nodes[key]
        node = content_node_from_object(store.get_object(object_id))
        content = (
            node.content
            if isinstance(node.content, str)
            else json.dumps(node.content, ensure_ascii=False, sort_keys=True)
        )
        nodes.append(
            {
                "key": key,
                "kind": node.kind,
                "title": node.title or None,
                "object_id": object_id,
                "source_scope": revision.source_scopes[key],
                "chars": len(content),
                "preview": content[:500],
            }
        )
    return {"agent": agent, "head": head_id, "nodes": nodes}


def _agent_content_log_item(store: Store, head_id: str) -> dict[str, Any]:
    revision = content_revision_from_object(store.get_object(head_id))
    reason = ""
    producer = ""
    for derivation in reversed(store.derivations_for_output(head_id)):
        if derivation.producer not in {"ContentAdvance:v1", "ContentRestore:v1"}:
            continue
        producer = derivation.producer
        value = derivation.params.get("reason")
        reason = value if isinstance(value, str) else ""
        break
    return {
        "head": head_id,
        "owner": revision.owner,
        "nodes": len(revision.nodes),
        "producer": producer,
        "reason": reason,
    }


def _agent_content_diff(
    agent: str,
    old_id: str,
    old: ContentRevision,
    new_id: str,
    new: ContentRevision,
) -> dict[str, Any]:
    old_keys = set(old.nodes)
    new_keys = set(new.nodes)
    added = [
        {"key": key, "object_id": new.nodes[key]}
        for key in new.projection_order
        if key in new_keys - old_keys
    ]
    removed = [
        {"key": key, "object_id": old.nodes[key]}
        for key in old.projection_order
        if key in old_keys - new_keys
    ]
    changed = [
        {
            "key": key,
            "old_object_id": old.nodes[key],
            "new_object_id": new.nodes[key],
        }
        for key in new.projection_order
        if key in old_keys and old.nodes[key] != new.nodes[key]
    ]
    return {
        "agent": agent,
        "old_head": old_id,
        "new_head": new_id,
        "added": added,
        "removed": removed,
        "changed": changed,
    }


def _agent_tool_list(
    store: Store,
    agent: str,
    revision: ContentRevision,
) -> list[dict[str, Any]]:
    result = []
    for key in revision.projection_order:
        object_id = revision.nodes[key]
        node = content_node_from_object(store.get_object(object_id))
        if node.kind != "tool_definition":
            continue
        definition = agent_tool_definition_from_content(
            node.content,
            owner=agent,
            key=key,
            object_id=object_id,
        )
        result.append(
            {
                "key": definition.key,
                "name": definition.name,
                "capability_id": definition.capability_id,
                "object_id": definition.object_id,
            }
        )
    return result


def _active_agent_tool(
    store: Store,
    agent: str,
    tool: str,
    revision: ContentRevision,
) -> AgentToolDefinition:
    key = tool if tool.startswith("tools/") else f"tools/{tool}"
    object_id = revision.nodes.get(key)
    if object_id is None:
        raise ContentValidationError(
            f"agent {agent!r} has no active graph tool {tool!r}"
        )
    node = content_node_from_object(store.get_object(object_id))
    if node.kind != "tool_definition":
        raise ContentValidationError(f"content key {key!r} is not a tool definition")
    return agent_tool_definition_from_content(
        node.content,
        owner=agent,
        key=key,
        object_id=object_id,
    )


def _resolve_agent_tool_version(
    store: Store,
    agent: str,
    tool: str,
    version: str,
) -> AgentToolDefinition:
    object_id = resolve_object_id(store, version)
    node = content_node_from_object(store.get_object(object_id))
    key = tool if tool.startswith("tools/") else f"tools/{tool}"
    if node.kind != "tool_definition" or node.key != key:
        raise ContentValidationError(
            f"content object {object_id!r} is not a version of {key!r}"
        )
    return agent_tool_definition_from_content(
        node.content,
        owner=agent,
        key=key,
        object_id=object_id,
    )


def _tool_version_in_history(
    store: Store,
    current_head: str,
    key: str,
    object_id: str,
) -> bool:
    for head_id in content_head_history(store, current_head, limit=1000):
        revision = content_revision_from_object(store.get_object(head_id))
        if revision.nodes.get(key) == object_id:
            return True
    return False


def _agent_tool_view(definition: AgentToolDefinition) -> dict[str, Any]:
    return {
        "owner": definition.owner,
        "key": definition.key,
        "name": definition.name,
        "capability_id": definition.capability_id,
        "object_id": definition.object_id,
        "source": definition.source,
    }


def _content_error(exc: Exception) -> str:
    if isinstance(exc, UnknownIdError):
        return f"unknown content revision {exc.token!r}"
    if isinstance(exc, AmbiguousIdError):
        return f"ambiguous content revision {exc.token!r}"
    return str(exc)


@agents.command("new")
@click.argument("slug")
@click.option("--name", default=None, help="Human-readable name.")
@click.option(
    "--description", default=None, help="One-line description / system prompt."
)
@click.option(
    "--accepts", multiple=True, help="Event type the agent accepts (repeatable)."
)
@click.option(
    "--tool", "tools", multiple=True, help="Capability the agent may use (repeatable)."
)
@click.option(
    "--skill", "skills", multiple=True, help="Shared skill the agent uses (repeatable)."
)
@click.option(
    "--base-dir", default=None, help="Base directory for relative file-tool paths."
)
@click.option(
    "--project-root",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("."),
    help="Project root containing agents/.",
)
@click.option("--force", is_flag=True, help="Overwrite an existing agent file.")
def agents_new(
    slug: str,
    name: str | None,
    description: str | None,
    accepts: tuple[str, ...],
    tools: tuple[str, ...],
    skills: tuple[str, ...],
    base_dir: str | None,
    project_root: Path,
    force: bool,
) -> None:
    """Scaffold agents/<slug>.md from a template."""
    from zeta.authoring.scaffold import ScaffoldError, scaffold_agent

    try:
        path = scaffold_agent(
            project_root,
            slug,
            name=name,
            description=description,
            accepts=accepts,
            tools=tools,
            skills=skills,
            base_dir=base_dir,
            overwrite=force,
        )
    except ScaffoldError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"created {path}")
