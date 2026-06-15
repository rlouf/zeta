"""Parallel-backed public web tools."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from zeta.tools.base import ToolSpec, error_result, write_temp

DEFAULT_BASE_URL = "https://api.parallel.ai"
DEFAULT_SEARCH_MODE = "basic"
DEFAULT_TIMEOUT_SEC = 30.0
DEFAULT_MAX_PREVIEW_BYTES = 8 * 1024
DEFAULT_MAX_PREVIEW_LINES = 100
DEFAULT_MAX_CHARS_TOTAL = 24_000

SEARCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["objective", "search_queries"],
    "properties": {
        "objective": {
            "type": "string",
            "description": "Self-contained natural-language search objective.",
        },
        "search_queries": {
            "type": "array",
            "description": "2-3 diverse keyword search queries, each 3-6 words.",
            "items": {"type": "string"},
            "minItems": 2,
            "maxItems": 3,
        },
    },
}

FETCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["urls"],
    "properties": {
        "urls": {
            "type": "array",
            "description": "Public URLs to fetch.",
            "items": {"type": "string"},
            "minItems": 1,
            "maxItems": 20,
        },
        "objective": {
            "type": "string",
            "description": (
                "Optional natural-language description of what to extract from "
                "the pages."
            ),
        },
    },
}

SEARCH_SPEC = ToolSpec(
    "web_search",
    (
        "Search public web pages using Parallel. Provide a self-contained "
        "objective and 2-3 keyword queries."
    ),
    SEARCH_SCHEMA,
    effects=("search",),
)

FETCH_SPEC = ToolSpec(
    "web_fetch",
    (
        "Fetch public URLs using Parallel and return clean Markdown. Use for "
        "known URLs; authenticated or private pages may fail."
    ),
    FETCH_SCHEMA,
    effects=("search",),
)


@dataclass(frozen=True)
class WebConfig:
    api_key: str
    base_url: str = DEFAULT_BASE_URL
    timeout_sec: float = DEFAULT_TIMEOUT_SEC
    max_preview_bytes: int = DEFAULT_MAX_PREVIEW_BYTES
    max_preview_lines: int = DEFAULT_MAX_PREVIEW_LINES
    search_mode: str = DEFAULT_SEARCH_MODE


@dataclass(frozen=True)
class Preview:
    text: str
    truncated: bool


def search(params: dict[str, Any]) -> dict[str, Any]:
    config = config_from_env()
    if config is None:
        return missing_key_error()
    objective = str(params.get("objective") or "").strip()
    search_queries = string_list(params.get("search_queries"))
    if not objective:
        return error_result("missing-objective", "web_search requires objective")
    if len(search_queries) < 2 or len(search_queries) > 3:
        return error_result(
            "invalid-search-queries",
            "web_search requires 2-3 search_queries",
        )
    payload: dict[str, Any] = {
        "objective": objective,
        "search_queries": search_queries,
        "mode": config.search_mode,
        "max_chars_total": DEFAULT_MAX_CHARS_TOTAL,
    }
    response = request_or_error("/v1/search", payload, config)
    if response.get("ok") is False:
        return response
    results = search_results(response)
    text = format_search_markdown(objective, results)
    preview = bounded_preview(
        text,
        max_bytes=config.max_preview_bytes,
        max_lines=config.max_preview_lines,
    )
    metadata = {
        "objective": objective,
        "search_queries": search_queries,
        "provider": "parallel",
        "search_id": text_or_none(response.get("search_id") or response.get("id")),
        "session_id": text_or_none(response.get("session_id")),
        "result_count": len(results),
        "truncated": preview.truncated,
    }
    return {
        "ok": True,
        "content": [{"type": "text", "text": preview.text}],
        "metadata": metadata,
    }


def fetch(params: dict[str, Any]) -> dict[str, Any]:
    config = config_from_env()
    if config is None:
        return missing_key_error()
    urls = string_list(params.get("urls"))
    if not urls or len(urls) > 20:
        return error_result("invalid-urls", "web_fetch requires 1-20 urls")
    objective = str(params.get("objective") or "").strip()
    payload: dict[str, Any] = {
        "urls": urls,
        "max_chars_total": DEFAULT_MAX_CHARS_TOTAL,
    }
    if objective:
        payload["objective"] = objective
    response = request_or_error("/v1/extract", payload, config)
    if response.get("ok") is False:
        return response
    pages = fetched_pages(response)
    url_errors = extract_errors(response)
    text = format_fetch_markdown(pages, url_errors)
    lines = count_lines(text)
    byte_count = len(text.encode("utf-8"))
    preview = bounded_preview(
        text,
        max_bytes=config.max_preview_bytes,
        max_lines=config.max_preview_lines,
    )
    metadata: dict[str, Any] = {
        "urls": urls,
        "provider": "parallel",
        "extract_id": text_or_none(response.get("extract_id") or response.get("id")),
        "session_id": text_or_none(response.get("session_id")),
        "bytes": byte_count,
        "lines": lines,
        "url_errors": url_errors,
        "truncated": preview.truncated,
    }
    if preview.truncated:
        path = write_temp("sigil_web_", ".md", text)
        metadata["output_path"] = str(path)
        shown = count_lines(preview.text)
        body = (
            f"web_fetch urls={', '.join(urls)}\n"
            f"output_path={path}\n\n"
            f"<head -{shown} {path}>\n"
            f"{preview.text}"
        )
        if body and not body.endswith("\n"):
            body += "\n"
        body += (
            "</head>\n"
            "Use read path=<output_path> start_line=<line> max_lines=<count> "
            "raw=true to inspect more."
        )
        content_text = body
    else:
        content_text = text
    return {
        "ok": True,
        "content": [{"type": "text", "text": content_text}],
        "metadata": metadata,
    }


def config_from_env() -> WebConfig | None:
    api_key = os.environ.get("PARALLEL_API_KEY", "").strip()
    if not api_key:
        return None
    return WebConfig(
        api_key=api_key,
        base_url=os.environ.get("PARALLEL_BASE_URL", DEFAULT_BASE_URL).rstrip("/"),
        timeout_sec=float(os.environ.get("SIGIL_WEB_TIMEOUT_SEC", DEFAULT_TIMEOUT_SEC)),
        max_preview_bytes=int(
            os.environ.get("SIGIL_WEB_MAX_PREVIEW_BYTES", DEFAULT_MAX_PREVIEW_BYTES)
        ),
        max_preview_lines=int(
            os.environ.get("SIGIL_WEB_MAX_PREVIEW_LINES", DEFAULT_MAX_PREVIEW_LINES)
        ),
        search_mode=os.environ.get("SIGIL_WEB_SEARCH_MODE", DEFAULT_SEARCH_MODE),
    )


def missing_key_error() -> dict[str, Any]:
    return error_result("parallel-api-key-missing", "PARALLEL_API_KEY is not set")


def request_or_error(
    endpoint: str, payload: dict[str, Any], config: WebConfig
) -> dict[str, Any]:
    try:
        return parallel_request(endpoint, payload, config)
    except TimeoutError as exc:
        return error_result("parallel-timeout", str(exc))
    except OSError as exc:
        return error_result("parallel-request-failed", str(exc))
    except ValueError as exc:
        return error_result("parallel-bad-response", str(exc))


def parallel_request(
    endpoint: str, payload: dict[str, Any], config: WebConfig
) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        config.base_url + endpoint,
        data=body,
        headers={
            "content-type": "application/json",
            "x-api-key": config.api_key,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=config.timeout_sec) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        message = exc.reason
        try:
            error_body = exc.read().decode("utf-8", errors="replace")
        except OSError:
            error_body = ""
        if error_body:
            message = error_body
        raise OSError(f"Parallel HTTP {exc.code}: {message}") from exc
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Parallel response was not a JSON object")
    return data


def search_results(response: dict[str, Any]) -> list[dict[str, str]]:
    raw_results = response.get("results")
    if not isinstance(raw_results, list):
        return []
    results = []
    for raw in raw_results:
        if not isinstance(raw, dict):
            continue
        url = first_text(raw, "url", "href", "link")
        title = first_text(raw, "title", "name")
        excerpt = result_excerpt(raw)
        if not url:
            continue
        results.append({"url": url, "title": title or url, "excerpt": excerpt})
    return results


def result_excerpt(raw: dict[str, Any]) -> str:
    for key in ("excerpt", "snippet", "summary", "text"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    excerpts = raw.get("excerpts")
    if isinstance(excerpts, list):
        parts = []
        for item in excerpts:
            if isinstance(item, str):
                parts.append(item.strip())
            elif isinstance(item, dict):
                text = first_text(item, "text", "excerpt", "snippet")
                if text:
                    parts.append(text)
        return " ".join(part for part in parts if part)
    return ""


def format_search_markdown(objective: str, results: list[dict[str, str]]) -> str:
    lines = ["# Web search results", "", f"Objective: {objective}", ""]
    if not results:
        lines.append("No results returned.")
        return "\n".join(lines)
    for index, result in enumerate(results, start=1):
        lines.append(
            f"{index}. [{escape_markdown_link(result['title'])}]({result['url']})"
        )
        if result["excerpt"]:
            lines.append(f"   {normalize_ws(result['excerpt'])}")
    return "\n".join(lines)


def fetched_pages(response: dict[str, Any]) -> list[dict[str, str]]:
    raw_results = response.get("results")
    if not isinstance(raw_results, list):
        return []
    pages = []
    for raw in raw_results:
        if not isinstance(raw, dict):
            continue
        url = first_text(raw, "url", "href", "link")
        title = first_text(raw, "title", "name")
        content = first_text(raw, "content", "markdown", "text", "body")
        if not content:
            continue
        pages.append(
            {"url": url, "title": title or url or "Fetched page", "content": content}
        )
    return pages


def extract_errors(response: dict[str, Any]) -> list[dict[str, str]]:
    raw_errors = response.get("errors")
    if not isinstance(raw_errors, list):
        return []
    errors = []
    for raw in raw_errors:
        if isinstance(raw, str):
            errors.append({"url": "", "message": raw})
        elif isinstance(raw, dict):
            errors.append(
                {
                    "url": first_text(raw, "url", "href", "link"),
                    "message": first_text(raw, "message", "error", "reason")
                    or "failed",
                }
            )
    return errors


def format_fetch_markdown(
    pages: list[dict[str, str]], errors: list[dict[str, str]]
) -> str:
    lines: list[str] = []
    for index, page in enumerate(pages):
        if index:
            lines.extend(["", "---", ""])
        lines.append(f"# {page['title']}")
        if page["url"]:
            lines.extend(["", f"URL: {page['url']}"])
        lines.extend(["", page["content"].strip()])
    if errors:
        if lines:
            lines.append("")
        lines.append("## Fetch errors")
        for error in errors:
            prefix = f"{error['url']}: " if error["url"] else ""
            lines.append(f"- {prefix}{error['message']}")
    if not lines:
        return "No content returned."
    return "\n".join(lines).strip() + "\n"


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


def string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def first_text(raw: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def text_or_none(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def count_lines(text: str) -> int:
    if not text:
        return 0
    return text.count("\n") + (not text.endswith("\n"))


def normalize_ws(text: str) -> str:
    return " ".join(text.split())


def escape_markdown_link(text: str) -> str:
    return text.replace("[", "\\[").replace("]", "\\]")
