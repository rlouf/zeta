"""Content graph operations and prompt transform contracts for Zeta."""

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from zeta.capabilities.registry import (
    AgentToolDefinitionError,
    validate_agent_tool_definition,
)
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
MAX_CONTENT_MANIFEST_ITEMS = 50
MAX_PROJECTED_CONTENT_CHARS = 50_000
PROMPT_CONTENT_KINDS = frozenset({"instruction", "procedure", "memory", "example"})


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
class ContentPromotion:
    """Delay durable scope changes until the owning attempt succeeds."""

    scope: ContentScope
    key: str
    object_id: ObjectId | None
    expected_head: ObjectId | None
    expected_object_id: ObjectId | None
    source_head: ObjectId
    reason: str


@dataclass(frozen=True)
class ContentPromotionResult:
    """Describe the exact durable ref change for authoring history."""

    scope: ContentScope
    key: str
    object_id: ObjectId | None
    old_head: ObjectId | None
    new_head: ObjectId
    reason: str


@dataclass(frozen=True)
class ContentTransformInput:
    """Expose selected graph values without exposing mutable workspace state."""

    key: str
    object_id: ObjectId
    node: ContentNode


@dataclass(frozen=True)
class ContentFinishResult:
    """Resolve a graph value only when the run selects it as its answer."""

    object_id: ObjectId
    content: str


@dataclass(frozen=True)
class ContentTransformResult:
    """Return references so transformation data stays outside the root context."""

    head: ObjectId
    output_ids: tuple[ObjectId, ...]
    promotions: tuple[ContentPromotion, ...] = ()


@dataclass(frozen=True)
class _ContentDestination:
    scope: ContentScope
    key: str | None
    kind: str | None
    expected_object_id: ObjectId | None


@dataclass(frozen=True)
class _ResolvedContentTransform:
    expected_head: ObjectId
    reason: str
    operation: Mapping[str, Any]
    destination: _ContentDestination
    revision: ContentRevision
    selected: tuple[tuple[str, ObjectId], ...]


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

    def prompt_components(
        self,
        *,
        max_chars: int = MAX_PROJECTED_CONTENT_CHARS,
    ) -> tuple[PromptComponent, ...]:
        """Project bounded content while retaining the exact source head in trace."""

        head_id = self.current_head()
        revision = content_revision_from_object(self.store.get_object(head_id))
        projected: list[PromptComponent] = []
        projected_keys: list[str] = []
        omitted_keys: list[str] = []
        used_chars = 0
        for key in revision.projection_order:
            object_id = revision.nodes[key]
            node = self._node(object_id)
            if node.kind not in PROMPT_CONTENT_KINDS:
                continue
            message = self._content_message(node)
            if used_chars + len(message) > max_chars:
                omitted_keys.append(key)
                continue
            used_chars += len(message)
            projected_keys.append(key)
            projected.append(
                PromptComponent(
                    kind=f"content_{node.kind}",
                    data={
                        "key": key,
                        "kind": node.kind,
                        "title": node.title,
                        "source_scope": revision.source_scopes[key],
                    },
                    message={"role": "system", "content": message},
                    source_object_id=object_id,
                    links=(object_id,),
                )
            )
        manifest_items = [
            self._manifest_item(key, revision)
            for key in revision.projection_order[:MAX_CONTENT_MANIFEST_ITEMS]
        ]
        manifest_text = self._manifest_message(
            head_id,
            manifest_items,
            total=len(revision.projection_order),
        )
        manifest = PromptComponent(
            kind="content_manifest",
            data={
                "head": head_id,
                "items": manifest_items,
                "total": len(revision.projection_order),
                "projected_keys": projected_keys,
                "omitted_keys": omitted_keys,
            },
            message={"role": "system", "content": manifest_text},
            source_object_id=head_id,
            links=(head_id,),
        )
        return (manifest, *projected)

    def transform(self, params: Mapping[str, Any]) -> ContentTransformResult:
        """Apply one typed operation and expose durable writes as requests."""

        resolved = self._resolve_transform(params)
        operation_type = _required_string(resolved.operation, "type")
        if operation_type == "drop":
            return self._drop(
                resolved.revision,
                resolved.selected,
                destination=resolved.destination,
                expected_head=resolved.expected_head,
                reason=resolved.reason,
            )
        node = self._derived_node(
            operation_type,
            resolved.operation,
            resolved.selected,
            destination=resolved.destination,
        )
        return self._store_derived_node(
            resolved.revision,
            node,
            resolved.selected,
            destination=resolved.destination,
            expected_head=resolved.expected_head,
            reason=resolved.reason,
            producer=f"Content{operation_type.title()}:v1",
            producer_params={"type": operation_type},
        )

    def transform_inputs(
        self,
        params: Mapping[str, Any],
    ) -> tuple[ContentTransformInput, ...]:
        """Resolve exact values before a model or Python transform leaves storage."""

        resolved = self._resolve_transform(params)
        return tuple(
            ContentTransformInput(key, object_id, self._node(object_id))
            for key, object_id in resolved.selected
        )

    def finish(self, object_id: ObjectId) -> ContentFinishResult:
        """Keep final reads inside the active graph instead of copying context."""

        closure = self.store.graph_closure([self.current_head()])
        obj = closure.get(object_id)
        if obj is None:
            raise ContentValidationError(
                "finished object must be reachable from the current content head"
            )
        if obj.kind == "content_node":
            value = content_node_from_object(obj).content
        elif obj.kind == "assistant_message":
            message = obj.data.get("message")
            value = message.get("content") if isinstance(message, Mapping) else None
        else:
            value = obj.data
        content = (
            value
            if isinstance(value, str)
            else json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        )
        return ContentFinishResult(object_id, content)

    def store_transformed_value(
        self,
        params: Mapping[str, Any],
        value: Any,
        *,
        source_ids: tuple[ObjectId, ...],
        producer: str,
        producer_params: Mapping[str, Any],
    ) -> ContentTransformResult:
        """Recheck the head before a slow derived value becomes active."""

        resolved = self._resolve_transform(params)
        if resolved.destination.key is None or resolved.destination.kind is None:
            raise ContentValidationError(
                "content destination key and kind are required"
            )
        title = str(resolved.operation.get("title") or "")
        attributes = resolved.operation.get("attributes")
        if attributes is not None and not isinstance(attributes, Mapping):
            raise ContentValidationError("content attributes must be an object")
        return self._store_derived_node(
            resolved.revision,
            ContentNode(
                resolved.destination.key,
                resolved.destination.kind,
                value,
                title=title,
                attributes=dict(attributes or {}),
            ),
            resolved.selected,
            destination=resolved.destination,
            expected_head=resolved.expected_head,
            reason=resolved.reason,
            extra_source_ids=source_ids,
            producer=producer,
            producer_params=producer_params,
        )

    def promote(self, promotion: ContentPromotion) -> ObjectId:
        """Make requested content durable only at the coordinator success boundary."""

        return self.promote_all((promotion,))[0].new_head

    def promote_all(
        self,
        promotions: tuple[ContentPromotion, ...],
    ) -> tuple[ContentPromotionResult, ...]:
        """Validate one attempt's baseline before its ordered changes become active."""

        grouped: dict[ContentScope, list[ContentPromotion]] = {}
        for promotion in promotions:
            if promotion.scope == "run":
                raise ContentValidationError("run content does not need promotion")
            grouped.setdefault(promotion.scope, []).append(promotion)
        results: list[ContentPromotionResult] = []
        with self.store.batch():
            for scope, requests in grouped.items():
                results.extend(self._promote_scope(scope, requests))
        return tuple(results)

    def _promote_scope(
        self,
        scope: ContentScope,
        requests: list[ContentPromotion],
    ) -> list[ContentPromotionResult]:
        target = self._head_for_scope(scope)
        current = self.store.get_ref(target.ref_name)
        head = current.object_id if current is not None else None
        revision = self._revision_or_empty(head)
        self._validate_promotion_baseline(target, head, revision, requests)
        results: list[ContentPromotionResult] = []
        for request in requests:
            next_revision = self._promoted_revision(revision, request)
            new_head = advance_content_head(
                self.store,
                target,
                expected_head=head,
                nodes=next_revision.nodes,
                projection_order=next_revision.projection_order,
                source_scopes=next_revision.source_scopes,
                reason=request.reason,
                source_ids=(request.source_head,),
            )
            results.append(
                ContentPromotionResult(
                    scope=request.scope,
                    key=request.key,
                    object_id=request.object_id,
                    old_head=head,
                    new_head=new_head,
                    reason=request.reason,
                )
            )
            head = new_head
            revision = next_revision
        return results

    def _validate_promotion_baseline(
        self,
        target: ContentHead,
        head: ObjectId | None,
        revision: ContentRevision,
        requests: list[ContentPromotion],
    ) -> None:
        for request in requests:
            if request.expected_head != head:
                raise ContentConflict(
                    f"content head {target.ref_name!r} changed from "
                    f"{request.expected_head!r} to {head!r}"
                )
            if revision.nodes.get(request.key) != request.expected_object_id:
                raise ContentConflict(
                    f"content object {request.key!r} changed before promotion"
                )

    def _promoted_revision(
        self,
        revision: ContentRevision,
        request: ContentPromotion,
    ) -> ContentRevision:
        nodes = dict(revision.nodes)
        order = list(revision.projection_order)
        sources = dict(revision.source_scopes)
        if request.object_id is None:
            nodes.pop(request.key, None)
            sources.pop(request.key, None)
            order = [key for key in order if key != request.key]
        else:
            if request.key not in nodes:
                order.append(request.key)
            nodes[request.key] = request.object_id
            sources[request.key] = request.scope
        return ContentRevision(
            self.run_head.owner,
            nodes,
            tuple(order),
            sources,
        )

    def _drop(
        self,
        revision: ContentRevision,
        selected: tuple[tuple[str, ObjectId], ...],
        *,
        destination: _ContentDestination,
        expected_head: ObjectId,
        reason: str,
    ) -> ContentTransformResult:
        key, object_id = _one_selected(selected, "drop")
        if destination.key is not None and destination.key != key:
            raise ContentValidationError("drop destination key must match its input")
        destination = _ContentDestination(
            destination.scope,
            key,
            None,
            destination.expected_object_id,
        )
        promotion = self._promotion_for(
            destination,
            object_id=None,
            source_head=expected_head,
            reason=reason,
        )
        nodes = dict(revision.nodes)
        nodes.pop(key)
        sources = dict(revision.source_scopes)
        sources.pop(key)
        order = tuple(item for item in revision.projection_order if item != key)
        head = advance_content_head(
            self.store,
            self.run_head,
            expected_head=expected_head,
            nodes=nodes,
            projection_order=order,
            source_scopes=sources,
            reason=reason,
            source_ids=(object_id,),
        )
        promotion = _promotion_with_source_head(promotion, head)
        return ContentTransformResult(
            head=head,
            output_ids=(),
            promotions=(() if promotion is None else (promotion,)),
        )

    def _store_derived_node(
        self,
        revision: ContentRevision,
        node: ContentNode,
        selected: tuple[tuple[str, ObjectId], ...],
        *,
        destination: _ContentDestination,
        expected_head: ObjectId,
        reason: str,
        extra_source_ids: tuple[ObjectId, ...] = (),
        producer: str,
        producer_params: Mapping[str, Any],
    ) -> ContentTransformResult:
        if destination.key is None:
            raise ContentValidationError("content destination key is required")
        if node.kind == "tool_definition":
            try:
                validate_agent_tool_definition(
                    node.content,
                    owner=self.run_head.owner,
                    key=node.key,
                )
            except AgentToolDefinitionError as exc:
                raise ContentValidationError(str(exc)) from exc
        promotion = self._promotion_for(
            destination,
            object_id=None,
            source_head=expected_head,
            reason=reason,
            validate_only=True,
        )
        source_ids = _unique_ids(
            *(object_id for _, object_id in selected),
            *extra_source_ids,
        )
        with self.store.batch():
            object_id = put_content_node(self.store, node, links=source_ids)
            self.store.record_derivation(
                Derivation(
                    producer=producer,
                    output_id=object_id,
                    input_ids=source_ids,
                    params=dict(producer_params),
                )
            )
            nodes = dict(revision.nodes)
            order = list(revision.projection_order)
            if destination.key not in nodes:
                order.append(destination.key)
            nodes[destination.key] = object_id
            sources = dict(revision.source_scopes)
            sources[destination.key] = "run"
            head = advance_content_head(
                self.store,
                self.run_head,
                expected_head=expected_head,
                nodes=nodes,
                projection_order=tuple(order),
                source_scopes=sources,
                reason=reason,
                source_ids=source_ids,
            )
            if promotion is not None:
                promotion = ContentPromotion(
                    scope=promotion.scope,
                    key=promotion.key,
                    object_id=object_id,
                    expected_head=promotion.expected_head,
                    expected_object_id=promotion.expected_object_id,
                    source_head=head,
                    reason=promotion.reason,
                )
        return ContentTransformResult(
            head=head,
            output_ids=(object_id,),
            promotions=(() if promotion is None else (promotion,)),
        )

    def _resolve_transform(
        self,
        params: Mapping[str, Any],
    ) -> _ResolvedContentTransform:
        expected_head = _required_string(params, "expected_head")
        current_head = self.current_head()
        if expected_head != current_head:
            raise ContentConflict(
                f"content head {self.run_head.ref_name!r} changed from "
                f"{expected_head!r} to {current_head!r}"
            )
        revision = self.revision()
        inputs = _mapping_field(params, "inputs")
        return _ResolvedContentTransform(
            expected_head=expected_head,
            reason=_required_string(params, "reason"),
            operation=_mapping_field(params, "transformation"),
            destination=_content_destination(_mapping_field(params, "destination")),
            revision=revision,
            selected=self._select(revision, inputs),
        )

    def _derived_node(
        self,
        operation_type: str,
        operation: Mapping[str, Any],
        selected: tuple[tuple[str, ObjectId], ...],
        *,
        destination: _ContentDestination,
    ) -> ContentNode:
        if destination.key is None or destination.kind is None:
            raise ContentValidationError(
                "content destination key and kind are required"
            )
        if operation_type == "literal":
            if "value" not in operation:
                raise ContentValidationError("literal transformation requires value")
            attributes = operation.get("attributes")
            if attributes is not None and not isinstance(attributes, Mapping):
                raise ContentValidationError("content attributes must be an object")
            return ContentNode(
                destination.key,
                destination.kind,
                operation["value"],
                title=str(operation.get("title") or ""),
                attributes=dict(attributes or {}),
            )
        key, object_id = _one_selected(selected, operation_type)
        source = self._node(object_id)
        if operation_type == "identity":
            return ContentNode(
                destination.key,
                destination.kind,
                source.content,
                title=source.title,
                attributes=dict(source.attributes or {}),
            )
        if operation_type != "patch":
            raise ContentValidationError(
                f"unsupported content transformation {operation_type!r}"
            )
        patch = _mapping_field(operation, "patch")
        unknown = set(patch) - {"content", "title", "attributes"}
        if unknown:
            raise ContentValidationError(
                f"unsupported content patch fields: {', '.join(sorted(unknown))}"
            )
        attributes = patch.get("attributes", source.attributes or {})
        if not isinstance(attributes, Mapping):
            raise ContentValidationError("content attributes must be an object")
        return ContentNode(
            destination.key,
            destination.kind,
            patch.get("content", source.content),
            title=str(patch.get("title", source.title)),
            attributes=dict(attributes),
        )

    def _select(
        self,
        revision: ContentRevision,
        inputs: Mapping[str, Any],
    ) -> tuple[tuple[str, ObjectId], ...]:
        raw_keys = inputs.get("keys")
        keys: list[str]
        if raw_keys is None:
            keys = (
                list(revision.projection_order)
                if "kind" in inputs or "source_scope" in inputs
                else []
            )
        elif isinstance(raw_keys, list) and all(
            isinstance(key, str) for key in raw_keys
        ):
            keys = list(dict.fromkeys(key for key in raw_keys if isinstance(key, str)))
        else:
            raise ContentValidationError("content input keys must be a string list")
        missing = [key for key in keys if key not in revision.nodes]
        if missing:
            raise ContentValidationError(f"unknown content key {missing[0]!r}")
        kind = inputs.get("kind")
        source_scope = inputs.get("source_scope")
        return tuple(
            (key, revision.nodes[key])
            for key in keys
            if (kind is None or self._node(revision.nodes[key]).kind == kind)
            and (source_scope is None or revision.source_scopes[key] == source_scope)
        )

    def _promotion_for(
        self,
        destination: _ContentDestination,
        *,
        object_id: ObjectId | None,
        source_head: ObjectId,
        reason: str,
        validate_only: bool = False,
    ) -> ContentPromotion | None:
        if destination.key is None:
            raise ContentValidationError("content destination key is required")
        target = self._head_for_scope(destination.scope)
        current = self.store.get_ref(target.ref_name)
        current_head = current.object_id if current is not None else None
        revision = self._revision_or_empty(current_head)
        if revision.nodes.get(destination.key) != destination.expected_object_id:
            raise ContentConflict(
                f"content object {destination.key!r} changed before transformation"
            )
        if destination.scope == "run":
            return None
        return ContentPromotion(
            scope=destination.scope,
            key=destination.key,
            object_id=None if validate_only else object_id,
            expected_head=current_head,
            expected_object_id=destination.expected_object_id,
            source_head=source_head,
            reason=reason,
        )

    def _head_for_scope(self, scope: ContentScope) -> ContentHead:
        if scope == "run":
            return self.run_head
        if scope == "session":
            return self.session_head
        if self.agent_head is None:
            raise ContentValidationError("agent content is unavailable for this run")
        return self.agent_head

    def _revision_or_empty(self, head_id: ObjectId | None) -> ContentRevision:
        if head_id is None:
            return ContentRevision(self.run_head.owner, {}, (), {})
        revision = content_revision_from_object(self.store.get_object(head_id))
        self._validate_owner(revision)
        return revision

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

    def _content_message(self, node: ContentNode) -> str:
        title = f"\nTitle: {node.title}" if node.title else ""
        return (
            f"Content key: {node.key}\nKind: {node.kind}{title}\n"
            f"{self._render_content(node.content)}"
        )

    def _manifest_item(
        self,
        key: str,
        revision: ContentRevision,
    ) -> dict[str, Any]:
        object_id = revision.nodes[key]
        node = self._node(object_id)
        rendered = self._render_content(node.content)
        return {
            "key": key,
            "kind": node.kind,
            "title": node.title,
            "source_scope": revision.source_scopes[key],
            "object_id": object_id,
            "chars": len(rendered),
        }

    def _manifest_message(
        self,
        head_id: ObjectId,
        items: list[dict[str, Any]],
        *,
        total: int,
    ) -> str:
        lines = [f"Content workspace head: {head_id}", "Available content:"]
        lines.extend(
            f"- {item['key']} ({item['kind']}, {item['source_scope']}, "
            f"{item['object_id']})"
            for item in items
        )
        if total > len(items):
            lines.append(f"- {total - len(items)} more items. Use query_content.")
        return "\n".join(lines)

    def _render_content(self, content: Any) -> str:
        if isinstance(content, str):
            return content
        return json.dumps(
            content,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )


def _required_string(value: Mapping[str, Any], field: str) -> str:
    item = value.get(field)
    if not isinstance(item, str) or not item.strip():
        raise ContentValidationError(f"content {field} must be a non-empty string")
    return item


def _mapping_field(value: Mapping[str, Any], field: str) -> Mapping[str, Any]:
    item = value.get(field)
    if not isinstance(item, Mapping):
        raise ContentValidationError(f"content {field} must be an object")
    return item


def content_promotion_from_mapping(value: Mapping[str, Any]) -> ContentPromotion:
    """Validate delayed writes again at the trusted coordinator boundary."""

    raw_scope = value.get("scope")
    if raw_scope == "session":
        scope: ContentScope = "session"
    elif raw_scope == "agent":
        scope = "agent"
    else:
        raise ContentValidationError("content promotion scope must be durable")
    return ContentPromotion(
        scope=scope,
        key=_content_key(_required_string(value, "key")),
        object_id=_optional_object_id(value.get("object_id"), "object_id"),
        expected_head=_optional_object_id(value.get("expected_head"), "expected_head"),
        expected_object_id=_optional_object_id(
            value.get("expected_object_id"),
            "expected_object_id",
        ),
        source_head=_required_string(value, "source_head"),
        reason=_required_string(value, "reason"),
    )


def _optional_object_id(value: Any, field: str) -> ObjectId | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ContentValidationError(f"content {field} must be a string")
    return value


def _content_destination(value: Mapping[str, Any]) -> _ContentDestination:
    if "scope" not in value:
        raise ContentValidationError("content destination scope is required")
    raw_scope = value.get("scope")
    if raw_scope == "run":
        scope: ContentScope = "run"
    elif raw_scope == "session":
        scope = "session"
    elif raw_scope == "agent":
        scope = "agent"
    else:
        raise ContentValidationError(f"unsupported content scope {raw_scope!r}")
    raw_key = value.get("key")
    key = None if raw_key is None else _content_key(str(raw_key))
    raw_kind = value.get("kind")
    kind = None if raw_kind is None else str(raw_kind)
    if "expected_object_id" not in value:
        raise ContentValidationError("content expected_object_id is required")
    expected_object_id = value.get("expected_object_id")
    if expected_object_id is not None and not isinstance(expected_object_id, str):
        raise ContentValidationError("content expected_object_id must be a string")
    return _ContentDestination(scope, key, kind, expected_object_id)


def _one_selected(
    selected: tuple[tuple[str, ObjectId], ...],
    operation: str,
) -> tuple[str, ObjectId]:
    if len(selected) != 1:
        raise ContentValidationError(
            f"{operation} transformation requires exactly one content input"
        )
    return selected[0]


def _promotion_with_source_head(
    promotion: ContentPromotion | None,
    source_head: ObjectId,
) -> ContentPromotion | None:
    if promotion is None:
        return None
    return ContentPromotion(
        scope=promotion.scope,
        key=promotion.key,
        object_id=promotion.object_id,
        expected_head=promotion.expected_head,
        expected_object_id=promotion.expected_object_id,
        source_head=source_head,
        reason=promotion.reason,
    )


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
