//! Formats injected public-web search results for agent consumption.

use std::future::Future;
use std::pin::Pin;

use serde_json::{json, Map, Value};

use crate::AbortSignal;

use super::{error_result, integer_param, object, string_param};

const DEFAULT_LIMIT: usize = 10;
const MAX_LIMIT: usize = 20;
const MAX_PREVIEW_BYTES: usize = 8 * 1024;
const MAX_PREVIEW_LINES: usize = 100;

/// Carries one asynchronous provider search result.
pub type WebSearchFuture<'a> = Pin<Box<dyn Future<Output = Result<WebSearchResult, String>> + 'a>>;

/// Carries one normalized public web source.
#[derive(Clone)]
pub struct WebSearchSource {
    /// Names the source for Markdown rendering.
    pub title: String,
    /// Carries the public source URL.
    pub url: String,
    /// Carries one bounded provider summary.
    pub snippet: String,
}

/// Carries one provider-neutral public web search response.
#[derive(Clone)]
pub struct WebSearchResult {
    /// Carries the provider's concise answer.
    pub answer: String,
    /// Lists cited public sources in provider order.
    pub sources: Vec<WebSearchSource>,
    /// Carries the provider request identifier when available.
    pub request_id: Option<String>,
    /// Carries the provider model when available.
    pub model: Option<String>,
    /// Carries normalized token usage when available.
    pub usage: Option<Map<String, Value>>,
}

/// Supplies a web-enabled Responses operation without implicit credentials.
///
/// # Examples
///
/// Implementations can adapt an application-owned Responses transport and
/// preserve its URL-citation annotations in [`WebSearchResult::sources`].
pub trait WebSearchProvider {
    /// Searches the public web with one explicit result limit.
    fn search<'a>(
        &'a mut self,
        query: &'a str,
        limit: usize,
        abort: &'a dyn AbortSignal,
    ) -> WebSearchFuture<'a>;
}

/// Rejects web search until an application injects a provider.
///
/// # Examples
///
/// ```
/// let provider = zeta_agent::UnavailableWebSearch;
/// let _ = provider;
/// ```
pub struct UnavailableWebSearch;

impl WebSearchProvider for UnavailableWebSearch {
    fn search<'a>(
        &'a mut self,
        _query: &'a str,
        _limit: usize,
        _abort: &'a dyn AbortSignal,
    ) -> WebSearchFuture<'a> {
        Box::pin(async { Err("no web search provider is configured".to_owned()) })
    }
}

pub(super) async fn web_search<W: WebSearchProvider>(
    params: &Map<String, Value>,
    provider: &mut W,
    abort: &dyn AbortSignal,
) -> Map<String, Value> {
    let query = string_param(params, "query");
    let query = query.trim();
    if query.is_empty() {
        return error_result("missing-query", "web_search requires query");
    }
    let limit = integer_param(params, "limit", DEFAULT_LIMIT);
    if limit == 0 || limit > MAX_LIMIT {
        return error_result("invalid-limit", "web_search limit must be 1-20");
    }
    let result = match provider.search(query, limit, abort).await {
        Ok(result) => result,
        Err(error) => return error_result("codex-request-failed", error),
    };
    let mut sources = result.sources;
    sources.truncate(limit);
    let text = format_markdown(query, &result.answer, &sources);
    let (text, truncated) = bounded_preview(&text, MAX_PREVIEW_BYTES, MAX_PREVIEW_LINES);
    object(json!({
        "ok": true,
        "content": [{"type": "text", "text": text}],
        "metadata": {
            "query": query,
            "provider": "codex",
            "request_id": result.request_id,
            "model": result.model,
            "result_count": sources.len(),
            "truncated": truncated,
            "usage": result.usage,
        },
    }))
}

fn format_markdown(query: &str, answer: &str, sources: &[WebSearchSource]) -> String {
    let mut lines = vec![
        "# Web search".to_owned(),
        String::new(),
        format!("Query: {query}"),
        String::new(),
    ];
    if !answer.is_empty() {
        lines.push(answer.trim().to_owned());
        lines.push(String::new());
    }
    if !sources.is_empty() {
        lines.push("## Sources".to_owned());
        for (index, source) in sources.iter().enumerate() {
            lines.push(format!(
                "[{}] [{}]({})",
                index + 1,
                escape_title(&source.title),
                source.url,
            ));
            if !source.snippet.is_empty() {
                let mut words = Vec::new();
                for word in source.snippet.split_whitespace() {
                    words.push(word);
                }
                let snippet = words.join(" ");
                let (snippet, _) = bounded_preview(&snippet, 240, usize::MAX);
                lines.push(format!("    {snippet}"));
            }
        }
    }
    format!("{}\n", lines.join("\n").trim())
}

fn bounded_preview(text: &str, max_bytes: usize, max_lines: usize) -> (String, bool) {
    let mut output = String::new();
    let mut bytes = 0;
    let mut lines = 0;
    for character in text.chars() {
        if bytes + character.len_utf8() > max_bytes || lines >= max_lines {
            return (output, true);
        }
        output.push(character);
        bytes += character.len_utf8();
        if character == '\n' {
            lines += 1;
        }
    }
    (output, false)
}

fn escape_title(title: &str) -> String {
    title.replace('[', "\\[").replace(']', "\\]")
}
