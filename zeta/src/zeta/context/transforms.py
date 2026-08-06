"""Content graph operations and prompt transform contracts for Zeta."""

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from zeta.context.budget import ContextUsage, measure
from zeta.context.components import PromptComponent
from zeta.substrate import Derivation, Object, ObjectId, Store

LOGGER = logging.getLogger("zeta.context")
_warned_over_budget = False

ContentScope = Literal["run", "session", "agent"]
CONTENT_SCOPES = frozenset({"run", "session", "agent"})
CONTENT_KINDS = frozenset(
    {
        "collection",
        "document",
        "evaluation",
        "example",
        "instruction",
        "json",
        "memory",
        "procedure",
        "text",
        "tool_definition",
    }
)
MAX_CONTENT_KEY_CHARS = 256
MAX_CONTENT_TITLE_CHARS = 512
MAX_CONTENT_NODE_BYTES = 256_000
MAX_CONTENT_ATTRIBUTES_BYTES = 32_000
MAX_CONTENT_QUERY_LIMIT = 50
MAX_CONTENT_PREVIEW_CHARS = 500


class ContentValidationError(ValueError):
    """Reject content that cannot remain stable across storage and replay."""


class ContentConflict(RuntimeError):
    """Protect newer content when a transformation used a stale head."""


@dataclass(frozen=True)
class ContentNode:
    """Keep one reusable value addressable without placing it in every prompt."""

    key: str
    kind: str
    content: Any
    title: str = ""
    attributes: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class ContentRevision:
    """Record one complete content view so a prompt can name its exact source."""

    owner: str
    nodes: Mapping[str, ObjectId]
    projection_order: tuple[str, ...]
    source_scopes: Mapping[str, str]


@dataclass(frozen=True)
class ContentHead:
    """Keep durable content refs explicit and separate from prompt trace refs."""

    scope: ContentScope
    scope_id: str
    owner: str

    def __post_init__(self) -> None:
        if self.scope not in CONTENT_SCOPES:
            raise ContentValidationError(f"unsupported content scope {self.scope!r}")
        if not self.scope_id.strip():
            raise ContentValidationError("content scope id must not be empty")
        if not self.owner.strip():
            raise ContentValidationError("content owner must not be empty")

    @property
    def ref_name(self) -> str:
        return f"{self.scope}/{self.scope_id}/content/head"


class ContentWorkspace:
    """Give one run an isolated head while it reads durable owner content."""

    def __init__(
        self,
        store: Store,
        *,
        run_id: str,
        session_id: str,
        owner: str,
        include_agent_content: bool = True,
    ) -> None:
        self.store = store
        self.run_head = ContentHead("run", run_id, owner)
        self.session_head = ContentHead("session", session_id, owner)
        self.agent_head = (
            ContentHead("agent", owner, owner) if include_agent_content else None
        )

    def initialize(self) -> ObjectId:
        """Snapshot durable heads once so retries and later turns use exact inputs."""

        existing = self.store.get_ref(self.run_head.ref_name)
        if existing is not None:
            revision = content_revision_from_object(
                self.store.get_object(existing.object_id)
            )
            self._validate_owner(revision)
            return existing.object_id
        nodes: dict[str, ObjectId] = {}
        order: list[str] = []
        sources: dict[str, str] = {}
        input_heads: list[ObjectId] = []
        if self.agent_head is not None:
            self._merge_durable_head(
                self.agent_head,
                nodes=nodes,
                order=order,
                sources=sources,
                input_heads=input_heads,
            )
        self._merge_durable_head(
            self.session_head,
            nodes=nodes,
            order=order,
            sources=sources,
            input_heads=input_heads,
        )
        return advance_content_head(
            self.store,
            self.run_head,
            expected_head=None,
            nodes=nodes,
            projection_order=tuple(order),
            source_scopes=sources,
            reason="Initialize the run content workspace.",
            source_ids=tuple(input_heads),
        )

    def current_head(self) -> ObjectId:
        current = self.store.get_ref(self.run_head.ref_name)
        return current.object_id if current is not None else self.initialize()

    def revision(self) -> ContentRevision:
        return content_revision_from_object(self.store.get_object(self.current_head()))

    def query(
        self,
        *,
        key_prefix: str | None = None,
        kind: str | None = None,
        source_scope: str | None = None,
        limit: int = 20,
        cursor: int | None = None,
    ) -> dict[str, Any]:
        """Return bounded previews so large content stays outside the root context."""

        if limit < 1 or limit > MAX_CONTENT_QUERY_LIMIT:
            raise ContentValidationError(
                f"content query limit must be from 1 to {MAX_CONTENT_QUERY_LIMIT}"
            )
        offset = 0 if cursor is None else cursor
        if offset < 0:
            raise ContentValidationError("content query cursor must not be negative")
        head_id = self.current_head()
        revision = content_revision_from_object(self.store.get_object(head_id))
        items = [
            self._query_item(key, revision)
            for key in revision.projection_order
            if (key_prefix is None or key.startswith(key_prefix))
            and (kind is None or self._node(revision.nodes[key]).kind == kind)
            and (source_scope is None or revision.source_scopes[key] == source_scope)
        ]
        page = items[offset : offset + limit]
        next_offset = offset + len(page)
        return {
            "head": head_id,
            "items": page,
            "next_cursor": next_offset if next_offset < len(items) else None,
        }

    def _merge_durable_head(
        self,
        head: ContentHead,
        *,
        nodes: dict[str, ObjectId],
        order: list[str],
        sources: dict[str, str],
        input_heads: list[ObjectId],
    ) -> None:
        current = self.store.get_ref(head.ref_name)
        if current is None:
            return
        revision = content_revision_from_object(
            self.store.get_object(current.object_id)
        )
        self._validate_owner(revision)
        input_heads.append(current.object_id)
        for key in revision.projection_order:
            if key not in nodes:
                order.append(key)
            nodes[key] = revision.nodes[key]
            sources[key] = head.scope

    def _validate_owner(self, revision: ContentRevision) -> None:
        if revision.owner != self.run_head.owner:
            raise ContentValidationError("content revision belongs to another owner")

    def _node(self, object_id: ObjectId) -> ContentNode:
        return content_node_from_object(self.store.get_object(object_id))

    def _query_item(
        self,
        key: str,
        revision: ContentRevision,
    ) -> dict[str, Any]:
        object_id = revision.nodes[key]
        node = self._node(object_id)
        rendered = (
            node.content
            if isinstance(node.content, str)
            else json.dumps(
                node.content,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        )
        return {
            "key": key,
            "kind": node.kind,
            "title": node.title,
            "object_id": object_id,
            "source_scope": revision.source_scopes[key],
            "chars": len(rendered),
            "preview": rendered[:MAX_CONTENT_PREVIEW_CHARS],
        }


def content_node_object(
    node: ContentNode,
    *,
    links: tuple[ObjectId, ...] = (),
) -> Object:
    """Store authored values in one schema so revisions can project them by kind."""

    key = _content_key(node.key)
    if node.kind not in CONTENT_KINDS:
        raise ContentValidationError(f"unsupported content kind {node.kind!r}")
    title = node.title.strip()
    if len(title) > MAX_CONTENT_TITLE_CHARS:
        raise ContentValidationError("content title is too long")
    attributes = dict(node.attributes or {})
    if _json_size(attributes) > MAX_CONTENT_ATTRIBUTES_BYTES:
        raise ContentValidationError("content attributes are too large")
    data = {
        "key": key,
        "kind": node.kind,
        "title": title,
        "content": node.content,
        "attributes": attributes,
    }
    if _json_size(data) > MAX_CONTENT_NODE_BYTES:
        raise ContentValidationError("content node is too large")
    return Object(
        kind="content_node",
        schema="zeta.content_node.v1",
        data=data,
        links=links,
    )


def content_node_from_object(obj: Object | None) -> ContentNode:
    """Fail closed when stored content does not match the supported schema."""

    if (
        obj is None
        or obj.kind != "content_node"
        or obj.schema != "zeta.content_node.v1"
    ):
        raise ContentValidationError("object is not a supported content node")
    data = obj.data
    attributes = data.get("attributes")
    if not isinstance(attributes, dict):
        raise ContentValidationError("content attributes must be an object")
    node = ContentNode(
        key=str(data.get("key") or ""),
        kind=str(data.get("kind") or ""),
        title=str(data.get("title") or ""),
        content=data.get("content"),
        attributes=attributes,
    )
    content_node_object(node, links=obj.links)
    return node


def put_content_node(
    store: Store,
    node: ContentNode,
    *,
    links: tuple[ObjectId, ...] = (),
) -> ObjectId:
    """Validate content before it can become reachable from an active revision."""

    for source_id in links:
        _require_object(store, source_id, "content source")
    return store.put_object(content_node_object(node, links=links))


def content_revision_object(revision: ContentRevision) -> Object:
    """Keep every active node in one deterministic and replayable snapshot."""

    owner = revision.owner.strip()
    if not owner:
        raise ContentValidationError("content revision owner must not be empty")
    nodes = dict(revision.nodes)
    for key, object_id in nodes.items():
        _content_key(key)
        if not isinstance(object_id, str) or not object_id:
            raise ContentValidationError(f"content node {key!r} has no object id")
    order = tuple(revision.projection_order)
    if len(order) != len(set(order)) or set(order) != set(nodes):
        raise ContentValidationError(
            "projection order must contain every content key exactly once"
        )
    sources = dict(revision.source_scopes)
    if set(sources) != set(nodes):
        raise ContentValidationError(
            "source scopes must contain every content key exactly once"
        )
    if any(scope not in CONTENT_SCOPES for scope in sources.values()):
        raise ContentValidationError("content revision has an unsupported source scope")
    return Object(
        kind="content_graph_revision",
        schema="zeta.content_graph_revision.v1",
        data={
            "owner": owner,
            "nodes": nodes,
            "projection_order": list(order),
            "source_scopes": sources,
        },
        links=tuple(nodes[key] for key in order),
    )


def content_revision_from_object(obj: Object | None) -> ContentRevision:
    """Reject partial revisions because prompts require one complete content view."""

    if (
        obj is None
        or obj.kind != "content_graph_revision"
        or obj.schema != "zeta.content_graph_revision.v1"
    ):
        raise ContentValidationError("object is not a supported content revision")
    data = obj.data
    raw_nodes = data.get("nodes")
    raw_order = data.get("projection_order")
    raw_sources = data.get("source_scopes")
    if (
        not isinstance(raw_nodes, dict)
        or not isinstance(raw_order, list)
        or not isinstance(raw_sources, dict)
    ):
        raise ContentValidationError("content revision fields are invalid")
    revision = ContentRevision(
        owner=str(data.get("owner") or ""),
        nodes={str(key): str(value) for key, value in raw_nodes.items()},
        projection_order=tuple(str(key) for key in raw_order),
        source_scopes={str(key): str(value) for key, value in raw_sources.items()},
    )
    expected = content_revision_object(revision)
    if expected.links != obj.links:
        raise ContentValidationError("content revision links do not match its nodes")
    return revision


def advance_content_head(
    store: Store,
    head: ContentHead,
    *,
    expected_head: ObjectId | None,
    nodes: Mapping[str, ObjectId],
    projection_order: tuple[str, ...],
    source_scopes: Mapping[str, str],
    reason: str,
    source_ids: tuple[ObjectId, ...] = (),
) -> ObjectId:
    """Use CAS so stale transformations cannot replace newer content."""

    revision = ContentRevision(
        owner=head.owner,
        nodes=dict(nodes),
        projection_order=projection_order,
        source_scopes=dict(source_scopes),
    )
    revision_object = content_revision_object(revision)
    _validate_revision_nodes(store, revision)
    for source_id in source_ids:
        _require_object(store, source_id, "content source")
    derivation_inputs = _unique_ids(
        *((expected_head,) if expected_head is not None else ()),
        *source_ids,
    )
    with store.batch():
        revision_id = store.put_object(revision_object)
        store.record_derivation(
            Derivation(
                producer="ContentAdvance:v1",
                output_id=revision_id,
                input_ids=derivation_inputs,
                params={
                    "owner": head.owner,
                    "reason": reason.strip(),
                    "scope": head.scope,
                    "scope_id": head.scope_id,
                },
            )
        )
        update = store.move_ref(head.ref_name, expected_head, revision_id)
        if not update.updated:
            raise ContentConflict(
                f"content head {head.ref_name!r} changed from {expected_head!r} "
                f"to {update.old_object_id!r}"
            )
    return revision_id


def restore_content_head(
    store: Store,
    head: ContentHead,
    *,
    expected_head: ObjectId,
    target_head: ObjectId,
    reason: str,
) -> ObjectId:
    """Restore by moving a ref so immutable history stays available for replay."""

    target = content_revision_from_object(store.get_object(target_head))
    if target.owner != head.owner:
        raise ContentValidationError("content revision belongs to another owner")
    with store.batch():
        store.record_derivation(
            Derivation(
                producer="ContentRestore:v1",
                output_id=target_head,
                input_ids=(expected_head,),
                params={
                    "owner": head.owner,
                    "reason": reason.strip(),
                    "scope": head.scope,
                    "scope_id": head.scope_id,
                },
            )
        )
        update = store.move_ref(head.ref_name, expected_head, target_head)
        if not update.updated:
            raise ContentConflict(
                f"content head {head.ref_name!r} changed from {expected_head!r} "
                f"to {update.old_object_id!r}"
            )
    return target_head


def content_head_history(
    store: Store,
    head_id: ObjectId,
    *,
    limit: int = 100,
) -> tuple[ObjectId, ...]:
    """Follow prior revision derivations without treating source evidence as heads."""

    history: list[ObjectId] = []
    current: ObjectId | None = head_id
    while current is not None and current not in history and len(history) < limit:
        content_revision_from_object(store.get_object(current))
        history.append(current)
        current = _prior_content_head(store, current)
    return tuple(history)


def _prior_content_head(store: Store, output_id: ObjectId) -> ObjectId | None:
    derivations = store.derivations_for_output(output_id)
    for derivation in reversed(derivations):
        if derivation.producer != "ContentAdvance:v1":
            continue
        for input_id in derivation.input_ids:
            obj = store.get_object(input_id)
            if obj is not None and obj.kind == "content_graph_revision":
                return input_id
    return None


def _validate_revision_nodes(store: Store, revision: ContentRevision) -> None:
    for key, object_id in revision.nodes.items():
        node = content_node_from_object(store.get_object(object_id))
        if node.key != key:
            raise ContentValidationError(
                f"content key {key!r} points to node {node.key!r}"
            )


def _require_object(store: Store, object_id: ObjectId, label: str) -> Object:
    obj = store.get_object(object_id)
    if obj is None:
        raise ContentValidationError(f"{label} {object_id!r} does not exist")
    return obj


def _content_key(value: str) -> str:
    key = value.strip()
    if not key:
        raise ContentValidationError("content key must not be empty")
    if key != value or len(key) > MAX_CONTENT_KEY_CHARS:
        raise ContentValidationError("content key is invalid")
    return key


def _json_size(value: Any) -> int:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ContentValidationError("content must contain valid JSON values") from exc
    return len(encoded)


def _unique_ids(*object_ids: ObjectId) -> tuple[ObjectId, ...]:
    return tuple(dict.fromkeys(object_ids))


class PromptTransform(Protocol):
    """Transform prompt components before the final model payload is built."""

    async def apply(
        self, components: list[PromptComponent]
    ) -> list[PromptComponent]: ...


class NoOpPromptTransform:
    """Default prompt transform that preserves current runtime behavior."""

    async def apply(self, components: list[PromptComponent]) -> list[PromptComponent]:
        return list(components)


@dataclass(frozen=True)
class BudgetThresholdPromptTransform:
    """Run a transform once measurement exceeds a threshold, then re-measure.

    Each escalation transform runs only while the prompt is still over
    budget; when the whole ladder is exhausted the overflow is signalled
    loudly instead of shipped silently.
    """

    transform: PromptTransform
    max_tokens: int
    escalation: tuple[PromptTransform, ...] = ()

    @property
    def producer(self) -> str:
        return str(getattr(self.transform, "producer", "") or "")

    async def apply(self, components: list[PromptComponent]) -> list[PromptComponent]:
        if measure(components).total_tokens <= self.max_tokens:
            return list(components)
        output = await self.transform.apply(components)
        for transform in self.escalation:
            if measure(output).total_tokens <= self.max_tokens:
                return output
            output = await transform.apply(output)
        usage = measure(output)
        if usage.total_tokens > self.max_tokens:
            warn_over_budget(usage, self.max_tokens)
        return output


def warn_over_budget(usage: ContextUsage, max_tokens: int) -> None:
    """Signal once per process that compaction could not reach the budget."""
    global _warned_over_budget
    if _warned_over_budget:
        return
    _warned_over_budget = True
    largest = max(usage.components, key=lambda component: component.tokens)
    LOGGER.warning(
        "prompt still over budget after compaction: ~%d tokens > %d budget "
        "(largest component: %s ~%d tokens)",
        usage.total_tokens,
        max_tokens,
        largest.kind,
        largest.tokens,
    )


def reset_over_budget_warning() -> None:
    """Re-arm the once-per-process over-budget warning."""
    global _warned_over_budget
    _warned_over_budget = False
