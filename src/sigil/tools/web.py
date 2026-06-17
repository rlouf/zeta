"""Codex-backed public web search tool."""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from zeta.models.chat_completions import iter_sse_data
from zeta.models.codex_auth import CodexCredentials, load_codex_credentials
from zeta.models.responses import codex_request_headers, codex_responses_url
from zeta.tools.base import CapabilityId, CapabilitySpec, error_result

DEFAULT_TIMEOUT_SEC = 30.0
DEFAULT_MAX_PREVIEW_BYTES = 8 * 1024
DEFAULT_MAX_PREVIEW_LINES = 100
DEFAULT_SEARCH_MODEL = "gpt-5.5"
DEFAULT_LIMIT = 10
DEFAULT_SEARCH_CONTEXT_SIZE = "high"

SEARCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["query"],
    "properties": {
        "query": {
            "type": "string",
            "description": "Self-contained public web search query.",
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": 20,
            "description": "Maximum number of source URLs to return.",
        },
    },
}

SEARCH_SPEC = CapabilitySpec(
    CapabilityId("sigil", "web_search"),
    (
        "Search public web pages using Codex hosted web search. Provide one "
        "self-contained query; use read for URLs returned by the search."
    ),
    SEARCH_SCHEMA,
    effects=("search",),
    aliases=("web_search",),
)


@dataclass(frozen=True)
class SearchSource:
    title: str
    url: str
    snippet: str = ""


@dataclass(frozen=True)
class CodexSearch:
    answer: str
    sources: list[SearchSource]
    request_id: str | None = None
    model: str | None = None
    usage: dict[str, int] | None = None


@dataclass(frozen=True)
class WebConfig:
    credentials: CodexCredentials
    model: str = DEFAULT_SEARCH_MODEL
    timeout_sec: float = DEFAULT_TIMEOUT_SEC
    max_preview_bytes: int = DEFAULT_MAX_PREVIEW_BYTES
    max_preview_lines: int = DEFAULT_MAX_PREVIEW_LINES
    limit: int = DEFAULT_LIMIT
    selected_url: str | None = None


@dataclass(frozen=True)
class Preview:
    text: str
    truncated: bool


@dataclass
class CodexSearchAccumulator:
    text_parts: list[str] = field(default_factory=list)
    streamed_parts: list[str] = field(default_factory=list)
    sources: list[SearchSource] = field(default_factory=list)
    request_id: str | None = None
    model: str | None = None
    usage: dict[str, int] | None = None

    def answer(self) -> str:
        answer = "\n\n".join(part for part in self.text_parts if part).strip()
        return answer or "".join(self.streamed_parts).strip()


def search(params: dict[str, Any]) -> dict[str, Any]:
    query = str(params.get("query") or "").strip()
    if not query:
        return error_result("missing-query", "web_search requires query")
    limit = int(params.get("limit") or DEFAULT_LIMIT)
    if limit < 1 or limit > 20:
        return error_result("invalid-limit", "web_search limit must be 1-20")
    try:
        config = config_from_env(limit=limit)
    except RuntimeError as exc:
        return error_result("codex-auth-missing", str(exc))
    response = request_or_error(query, config)
    if response.get("ok") is False:
        return response
    result = response["result"]
    if not isinstance(result, CodexSearch):
        return error_result("codex-bad-response", "Codex search response was invalid")
    sources = result.sources[:limit]
    text = format_search_markdown(query, result.answer, sources)
    preview = bounded_preview(
        text,
        max_bytes=config.max_preview_bytes,
        max_lines=config.max_preview_lines,
    )
    return {
        "ok": True,
        "content": [{"type": "text", "text": preview.text}],
        "metadata": {
            "query": query,
            "provider": "codex",
            "request_id": result.request_id,
            "model": result.model,
            "result_count": len(sources),
            "truncated": preview.truncated,
            "usage": result.usage,
        },
    }


def config_from_env(*, limit: int) -> WebConfig:
    credentials = load_codex_credentials()
    return WebConfig(
        credentials=credentials,
        model=os.environ.get("SIGIL_WEB_SEARCH_MODEL", DEFAULT_SEARCH_MODEL),
        timeout_sec=float(os.environ.get("SIGIL_WEB_TIMEOUT_SEC", DEFAULT_TIMEOUT_SEC)),
        max_preview_bytes=int(
            os.environ.get("SIGIL_WEB_MAX_PREVIEW_BYTES", DEFAULT_MAX_PREVIEW_BYTES)
        ),
        max_preview_lines=int(
            os.environ.get("SIGIL_WEB_MAX_PREVIEW_LINES", DEFAULT_MAX_PREVIEW_LINES)
        ),
        limit=limit,
        selected_url=os.environ.get("SIGIL_CODEX_BASE_URL"),
    )


def request_or_error(query: str, config: WebConfig) -> dict[str, Any]:
    try:
        return {"ok": True, "result": codex_search(query, config)}
    except TimeoutError as exc:
        return error_result("codex-timeout", str(exc))
    except OSError as exc:
        return error_result("codex-request-failed", str(exc))
    except ValueError as exc:
        return error_result("codex-bad-response", str(exc))


def codex_search(query: str, config: WebConfig) -> CodexSearch:
    body = {
        "model": config.model,
        "stream": True,
        "store": False,
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": query}],
            }
        ],
        "tools": [
            {
                "type": "web_search",
                "search_context_size": DEFAULT_SEARCH_CONTEXT_SIZE,
            }
        ],
        "tool_choice": {"type": "web_search"},
        "instructions": (
            "Search the public web and answer concisely. Cite sources with "
            "URL citations when possible."
        ),
    }
    request = urllib.request.Request(
        codex_responses_url(config.selected_url),
        data=json.dumps(body).encode("utf-8"),
        headers=codex_request_headers(config.credentials, "sigil-web-search"),
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=config.timeout_sec) as response:
            return parse_codex_search_events(response)
    except urllib.error.HTTPError as exc:
        message = exc.reason
        try:
            error_body = exc.read().decode("utf-8", errors="replace")
        except OSError:
            error_body = ""
        if error_body:
            message = error_body
        raise OSError(f"Codex HTTP {exc.code}: {message}") from exc


def parse_codex_search_events(chunks: Iterable[bytes]) -> CodexSearch:
    acc = CodexSearchAccumulator()
    for payload in iter_sse_data(chunks):
        if payload == "[DONE]":
            break
        handle_codex_event(load_codex_event(payload), acc)
    answer = acc.answer()
    if not answer and not acc.sources:
        raise ValueError("Codex search returned no answer or sources")
    sources = acc.sources or extract_text_sources(answer)
    return CodexSearch(
        answer=answer,
        sources=sources,
        request_id=acc.request_id,
        model=acc.model,
        usage=acc.usage,
    )


def load_codex_event(payload: str) -> dict[str, Any]:
    event = json.loads(payload)
    if not isinstance(event, dict):
        raise ValueError("Codex stream event was not a JSON object")
    return event


def handle_codex_event(event: dict[str, Any], acc: CodexSearchAccumulator) -> None:
    event_type = str(event.get("type") or "")
    if event_type == "error":
        raise ValueError(format_event_error(event))
    if event_type == "response.failed":
        raise ValueError(format_response_failure(event))
    if event_type == "response.output_text.delta":
        collect_streamed_delta(event, acc)
    elif event_type == "response.output_item.done":
        collect_output_item(event.get("item"), acc.text_parts, acc.sources)
    elif event_type in {"response.completed", "response.done"}:
        collect_response_metadata(event, acc)


def collect_streamed_delta(
    event: dict[str, Any],
    acc: CodexSearchAccumulator,
) -> None:
    delta = event.get("delta")
    if isinstance(delta, str):
        acc.streamed_parts.append(delta)


def collect_response_metadata(
    event: dict[str, Any],
    acc: CodexSearchAccumulator,
) -> None:
    response = event.get("response")
    if not isinstance(response, dict):
        return
    acc.request_id = text_or_none(response.get("id")) or acc.request_id
    acc.model = text_or_none(response.get("model")) or acc.model
    acc.usage = response_usage(response.get("usage")) or acc.usage


def collect_output_item(
    item: Any,
    text_parts: list[str],
    sources: list[SearchSource],
) -> None:
    if not isinstance(item, dict) or item.get("type") != "message":
        return
    content = item.get("content")
    if not isinstance(content, list):
        return
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "output_text" and isinstance(part.get("text"), str):
            text_parts.append(part["text"])
        annotations = part.get("annotations")
        if isinstance(annotations, list):
            collect_annotations(annotations, sources)


def collect_annotations(annotations: list[Any], sources: list[SearchSource]) -> None:
    for annotation in annotations:
        if not isinstance(annotation, dict):
            continue
        if annotation.get("type") != "url_citation":
            continue
        url = text_or_none(annotation.get("url"))
        if not url:
            continue
        title = text_or_none(annotation.get("title")) or url
        add_source(sources, SearchSource(title=title, url=url))


def format_search_markdown(
    query: str,
    answer: str,
    sources: list[SearchSource],
) -> str:
    lines = ["# Web search", "", f"Query: {query}", ""]
    if answer:
        lines.extend([answer.strip(), ""])
    if sources:
        lines.append("## Sources")
        for index, source in enumerate(sources, start=1):
            lines.append(
                f"[{index}] [{escape_markdown_link(source.title)}]({source.url})"
            )
            if source.snippet:
                lines.append(f"    {normalize_ws(source.snippet)[:240]}")
    return "\n".join(lines).strip() + "\n"


def extract_text_sources(text: str) -> list[SearchSource]:
    sources: list[SearchSource] = []
    for match in re.finditer(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", text):
        add_source(
            sources, SearchSource(title=match.group(1), url=clean_url(match.group(2)))
        )
    for match in re.finditer(r"https?://\S+", text):
        url = clean_url(match.group(0))
        add_source(sources, SearchSource(title=url, url=url))
    return sources


def add_source(sources: list[SearchSource], source: SearchSource) -> None:
    if not source.url or any(existing.url == source.url for existing in sources):
        return
    sources.append(source)


def clean_url(value: str) -> str:
    return value.rstrip('.,;:!?)"]}')


def response_usage(value: Any) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    keys = {
        "input_tokens": "input_tokens",
        "output_tokens": "output_tokens",
        "total_tokens": "total_tokens",
    }
    usage = {
        output: count
        for key, output in keys.items()
        if isinstance((count := value.get(key)), int) and not isinstance(count, bool)
    }
    return usage or None


def format_event_error(event: dict[str, Any]) -> str:
    message = event.get("message")
    if isinstance(message, str) and message:
        return message
    return json.dumps(event, sort_keys=True)


def format_response_failure(event: dict[str, Any]) -> str:
    response = event.get("response")
    response = response if isinstance(response, dict) else {}
    error = response.get("error")
    error = error if isinstance(error, dict) else {}
    message = error.get("message")
    return message if isinstance(message, str) and message else "Codex search failed"


def bounded_preview(text: str, *, max_bytes: int, max_lines: int) -> Preview:
    used = 0
    lines = 0
    chars = []
    truncated = False
    for char in text:
        encoded_len = len(char.encode("utf-8"))
        if used + encoded_len > max_bytes or lines >= max_lines:
            truncated = True
            break
        chars.append(char)
        used += encoded_len
        if char == "\n":
            lines += 1
    return Preview("".join(chars), truncated or len(chars) < len(text))


def text_or_none(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def normalize_ws(text: str) -> str:
    return " ".join(text.split())


def escape_markdown_link(text: str) -> str:
    return text.replace("[", "\\[").replace("]", "\\]")
