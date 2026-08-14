//! Calls the Codex standalone web search endpoint.

use std::collections::BTreeMap;
use std::future::Future;
use std::pin::Pin;
use std::time::Duration;

use reqwest::header::{HeaderMap, HeaderName, HeaderValue, ACCEPT, CONTENT_TYPE};
use serde::Deserialize;
use serde_json::{json, Map, Value};

use crate::{AbortSignal, AgentError};

use super::{bounded_output, check_abort, error_result, object};

const SEARCH_TIMEOUT: Duration = Duration::from_secs(60);
const ABORT_POLL_INTERVAL: Duration = Duration::from_millis(10);
const MAX_OUTPUT_CHARS: usize = 24_000;
const MAX_SOURCE_CHARS: usize = 4_000;
const DEFAULT_SOURCE_LIMIT: usize = 10;
const MAX_SOURCE_LIMIT: usize = 20;
const MAX_OUTPUT_TOKENS: u64 = 4_096;

/// Configures one Codex standalone web search client.
#[derive(Clone, Debug)]
pub struct WebSearchConfig {
    endpoint: String,
    model: String,
    session_id: String,
    headers: BTreeMap<String, String>,
}

impl WebSearchConfig {
    /// Creates a search configuration from a Codex Responses endpoint.
    ///
    /// # Errors
    ///
    /// Returns [`AgentError`] when the endpoint has no `/responses` suffix.
    pub fn from_responses_endpoint(
        responses_endpoint: &str,
        model: impl Into<String>,
        session_id: impl Into<String>,
    ) -> Result<Self, AgentError> {
        let endpoint = responses_endpoint.trim_end_matches('/');
        let Some(endpoint) = endpoint.strip_suffix("/responses") else {
            return Err(AgentError::tool(
                "web search needs a Codex Responses endpoint that ends with /responses",
            ));
        };
        Ok(WebSearchConfig {
            endpoint: format!("{endpoint}/alpha/search"),
            model: model.into(),
            session_id: session_id.into(),
            headers: BTreeMap::new(),
        })
    }

    /// Adds the caller-resolved request headers.
    pub fn with_headers(mut self, headers: BTreeMap<String, String>) -> Self {
        self.headers = headers;
        self
    }
}

/// Sends one standalone Codex search request.
pub(super) struct WebSearchClient {
    config: WebSearchConfig,
    client: reqwest::Client,
}

impl WebSearchClient {
    pub(super) fn new(config: WebSearchConfig) -> Result<Self, AgentError> {
        let client = reqwest::Client::builder()
            .connect_timeout(SEARCH_TIMEOUT)
            .build()
            .map_err(|error| AgentError::tool(format!("web search client failed: {error}")))?;
        Ok(WebSearchClient { config, client })
    }

    pub(super) async fn execute(
        &self,
        params: &Map<String, Value>,
        abort: &dyn AbortSignal,
    ) -> Result<Map<String, Value>, AgentError> {
        check_abort(abort)?;
        let command = match WebCommand::parse(params) {
            Ok(command) => command,
            Err(message) => return Ok(error_result("invalid-web-search-input", message)),
        };
        let body = request_body(&self.config, &command);
        let headers = request_headers(&self.config.headers)?;
        let response = wait_for(
            self.client
                .post(&self.config.endpoint)
                .headers(headers)
                .json(&body)
                .send(),
            abort,
        )
        .await?
        .map_err(|error| AgentError::tool(format!("web search request failed: {error}")))?;
        let status = response.status();
        let bytes = wait_for(response.bytes(), abort)
            .await?
            .map_err(|error| AgentError::tool(format!("web search response failed: {error}")))?;
        if !status.is_success() {
            return Ok(error_result(
                "web-search-failed",
                format!(
                    "Codex web search returned HTTP {}: {}",
                    status.as_u16(),
                    response_error_detail(&bytes)
                ),
            ));
        }
        let response: SearchResponse = match serde_json::from_slice(&bytes) {
            Ok(response) => response,
            Err(error) => {
                return Ok(error_result(
                    "web-search-bad-response",
                    format!("Codex web search returned invalid JSON: {error}"),
                ));
            }
        };
        let (text, text_truncated) = bounded_output(&response.output, MAX_OUTPUT_CHARS);
        let (sources, sources_truncated, result_count) =
            bounded_sources(response.results, command.source_limit());
        Ok(object(json!({
            "ok": true,
            "content": [{"type": "text", "text": text}],
            "metadata": {
                "provider": "codex",
                "action": command.action(),
                "request_id": self.config.session_id,
                "model": self.config.model,
                "result_count": result_count,
                "truncated": text_truncated || sources_truncated,
                "sources": sources,
            },
        })))
    }
}

#[derive(Deserialize)]
struct SearchResponse {
    output: String,
    #[serde(default)]
    results: Option<Vec<Value>>,
}

enum WebCommand {
    Search {
        query: String,
        domains: Option<Vec<String>>,
        recency: Option<u64>,
        limit: usize,
    },
    Open {
        url: String,
        line: Option<u64>,
        limit: usize,
    },
    Find {
        url: String,
        pattern: String,
        limit: usize,
    },
}

impl WebCommand {
    fn parse(params: &Map<String, Value>) -> Result<Self, String> {
        let limit = source_limit(params)?;
        if params.contains_key("query") {
            if params.contains_key("url") || params.contains_key("pattern") {
                return Err("web search accepts query or url, not both".to_owned());
            }
            return Ok(WebCommand::Search {
                query: required_string(params, "query")?,
                domains: string_array(params, "domains")?,
                recency: optional_u64(params, "recency")?,
                limit,
            });
        }
        let url = required_string(params, "url")?;
        if params.contains_key("pattern") {
            return Ok(WebCommand::Find {
                url,
                pattern: required_string(params, "pattern")?,
                limit,
            });
        }
        Ok(WebCommand::Open {
            url,
            line: optional_u64(params, "line")?,
            limit,
        })
    }

    fn action(&self) -> &'static str {
        match self {
            WebCommand::Search { .. } => "search",
            WebCommand::Open { .. } => "open_page",
            WebCommand::Find { .. } => "find_in_page",
        }
    }

    fn source_limit(&self) -> usize {
        match self {
            WebCommand::Search { limit, .. }
            | WebCommand::Open { limit, .. }
            | WebCommand::Find { limit, .. } => *limit,
        }
    }

    fn input(&self) -> String {
        match self {
            WebCommand::Search { query, .. } => query.clone(),
            WebCommand::Open { url, .. } => format!("Open {url}"),
            WebCommand::Find { url, pattern, .. } => format!("Find {pattern:?} in {url}"),
        }
    }

    fn commands(&self) -> Value {
        match self {
            WebCommand::Search {
                query,
                domains,
                recency,
                ..
            } => {
                let mut search = Map::new();
                search.insert("q".to_owned(), Value::String(query.clone()));
                if let Some(domains) = domains {
                    search.insert(
                        "domains".to_owned(),
                        Value::Array(domains.iter().cloned().map(Value::String).collect()),
                    );
                }
                if let Some(recency) = recency {
                    search.insert("recency".to_owned(), json!(recency));
                }
                json!({"search_query": [Value::Object(search)]})
            }
            WebCommand::Open { url, line, .. } => {
                let mut open = Map::new();
                open.insert("ref_id".to_owned(), Value::String(url.clone()));
                if let Some(line) = line {
                    open.insert("lineno".to_owned(), json!(line));
                }
                json!({"open": [Value::Object(open)]})
            }
            WebCommand::Find { url, pattern, .. } => json!({
                "find": [{"ref_id": url, "pattern": pattern}],
            }),
        }
    }
}

fn request_body(config: &WebSearchConfig, command: &WebCommand) -> Value {
    json!({
        "id": config.session_id,
        "model": config.model,
        "input": command.input(),
        "commands": command.commands(),
        "settings": {"allowed_callers": ["direct"]},
        "max_output_tokens": MAX_OUTPUT_TOKENS,
    })
}

fn request_headers(headers: &BTreeMap<String, String>) -> Result<HeaderMap, AgentError> {
    let mut resolved = HeaderMap::new();
    resolved.insert(ACCEPT, HeaderValue::from_static("application/json"));
    resolved.insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
    for (name, value) in headers {
        let name = HeaderName::from_bytes(name.as_bytes()).map_err(|_| {
            AgentError::tool(format!("web search has an invalid header name {name}"))
        })?;
        if name == ACCEPT || name == CONTENT_TYPE {
            continue;
        }
        let value = HeaderValue::from_str(value).map_err(|_| {
            AgentError::tool(format!("web search has an invalid value for header {name}"))
        })?;
        resolved.insert(name, value);
    }
    Ok(resolved)
}

async fn wait_for<F, T>(future: F, abort: &dyn AbortSignal) -> Result<T, AgentError>
where
    F: Future<Output = T>,
{
    let mut future = Box::pin(future);
    let deadline = tokio::time::sleep(SEARCH_TIMEOUT);
    let mut deadline = Pin::from(Box::new(deadline));
    loop {
        check_abort(abort)?;
        tokio::select! {
            output = &mut future => return Ok(output),
            () = &mut deadline => return Err(AgentError::tool("web search request timed out")),
            () = tokio::time::sleep(ABORT_POLL_INTERVAL) => {}
        }
    }
}

fn required_string(params: &Map<String, Value>, name: &str) -> Result<String, String> {
    let Some(value) = params.get(name).and_then(Value::as_str) else {
        return Err(format!("web search requires {name}"));
    };
    let value = value.trim();
    if value.is_empty() {
        return Err(format!("web search requires a non-empty {name}"));
    }
    Ok(value.to_owned())
}

fn string_array(params: &Map<String, Value>, name: &str) -> Result<Option<Vec<String>>, String> {
    let Some(value) = params.get(name) else {
        return Ok(None);
    };
    let Some(values) = value.as_array() else {
        return Err(format!("web search {name} must be an array of strings"));
    };
    let mut strings = Vec::with_capacity(values.len());
    for value in values {
        let Some(value) = value.as_str().filter(|value| !value.trim().is_empty()) else {
            return Err(format!("web search {name} must be an array of strings"));
        };
        strings.push(value.to_owned());
    }
    Ok(Some(strings))
}

fn optional_u64(params: &Map<String, Value>, name: &str) -> Result<Option<u64>, String> {
    let Some(value) = params.get(name) else {
        return Ok(None);
    };
    value
        .as_u64()
        .map(Some)
        .ok_or_else(|| format!("web search {name} must be a non-negative integer"))
}

fn source_limit(params: &Map<String, Value>) -> Result<usize, String> {
    let Some(limit) = optional_u64(params, "limit")? else {
        return Ok(DEFAULT_SOURCE_LIMIT);
    };
    let limit = usize::try_from(limit)
        .map_err(|_| format!("web search limit must be 1-{MAX_SOURCE_LIMIT}"))?;
    if !(1..=MAX_SOURCE_LIMIT).contains(&limit) {
        return Err(format!("web search limit must be 1-{MAX_SOURCE_LIMIT}"));
    }
    Ok(limit)
}

fn bounded_sources(results: Option<Vec<Value>>, limit: usize) -> (Vec<Value>, bool, usize) {
    let results = results.unwrap_or_default();
    let result_count = results.len();
    let mut truncated = result_count > limit;
    let sources = results
        .into_iter()
        .take(limit)
        .map(|result| {
            let Ok(encoded) = serde_json::to_string(&result) else {
                truncated = true;
                return json!({"truncated": true, "preview": "source could not be encoded"});
            };
            let (preview, result_truncated) = bounded_output(&encoded, MAX_SOURCE_CHARS);
            if result_truncated {
                truncated = true;
                json!({"truncated": true, "preview": preview})
            } else {
                result
            }
        })
        .collect();
    (sources, truncated, result_count)
}

fn response_error_detail(body: &[u8]) -> String {
    let body = if body.len() > 2_048 {
        &body[..2_048]
    } else {
        body
    };
    if let Ok(value) = serde_json::from_slice::<Value>(body) {
        if let Some(message) = value
            .get("error")
            .and_then(Value::as_object)
            .and_then(|error| error.get("message"))
            .and_then(Value::as_str)
        {
            return message.to_owned();
        }
        if let Some(message) = value.get("message").and_then(Value::as_str) {
            return message.to_owned();
        }
        return value.to_string();
    }
    String::from_utf8_lossy(body).trim().to_owned()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn config() -> WebSearchConfig {
        WebSearchConfig::from_responses_endpoint(
            "https://chatgpt.com/backend-api/codex/responses",
            "gpt-test",
            "session-test",
        )
        .unwrap()
    }

    #[test]
    fn config_uses_the_codex_standalone_endpoint() {
        assert_eq!(
            config().endpoint,
            "https://chatgpt.com/backend-api/codex/alpha/search"
        );
    }

    #[test]
    fn search_request_uses_a_standalone_search_command() {
        let command = WebCommand::parse(
            json!({"query": "OpenAI news", "domains": ["openai.com"], "recency": 7})
                .as_object()
                .unwrap(),
        )
        .unwrap();
        assert_eq!(
            request_body(&config(), &command),
            json!({
                "id": "session-test",
                "model": "gpt-test",
                "input": "OpenAI news",
                "commands": {"search_query": [{
                    "q": "OpenAI news",
                    "domains": ["openai.com"],
                    "recency": 7,
                }]},
                "settings": {"allowed_callers": ["direct"]},
                "max_output_tokens": 4096,
            })
        );
    }

    #[test]
    fn page_commands_select_open_or_find() {
        let open = WebCommand::parse(
            json!({"url": "https://example.com", "line": 8})
                .as_object()
                .unwrap(),
        )
        .unwrap();
        assert_eq!(open.action(), "open_page");
        assert_eq!(
            open.commands(),
            json!({"open": [{"ref_id": "https://example.com", "lineno": 8}]})
        );

        let find = WebCommand::parse(
            json!({"url": "turn0search0", "pattern": "install"})
                .as_object()
                .unwrap(),
        )
        .unwrap();
        assert_eq!(find.action(), "find_in_page");
        assert_eq!(
            find.commands(),
            json!({"find": [{"ref_id": "turn0search0", "pattern": "install"}]})
        );
    }

    #[test]
    fn source_records_keep_their_shape_until_the_bound() {
        let (sources, truncated, count) = bounded_sources(
            Some(vec![
                json!({"ref_id": "turn0search0", "url": "https://example.com"}),
            ]),
            10,
        );
        assert_eq!(count, 1);
        assert!(!truncated);
        assert_eq!(
            sources,
            vec![json!({"ref_id": "turn0search0", "url": "https://example.com"})]
        );
    }
}
