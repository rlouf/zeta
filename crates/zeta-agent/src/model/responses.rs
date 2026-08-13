//! Translates Responses requests and streamed response events.

use std::collections::BTreeMap;

use serde_json::{json, Map, Value};

use super::sse::{decode_stream_event, format_stream_error};
use super::{DecodedModelStream, ModelInput, Observation};
use crate::error::AgentError;

const RESPONSES_ITEMS_FIELD: &str = "_responses_items";
const REASONING_SUMMARY_SEPARATOR: &str = "\n\n";

/// Carries the credentials needed to form Codex request headers.
#[derive(Clone, PartialEq, Eq)]
pub struct CodexCredentials {
    access_token: String,
    account_id: String,
}

impl CodexCredentials {
    /// Creates credentials from one access token and account identifier.
    ///
    /// # Examples
    ///
    /// ```
    /// let credentials = zeta_agent::CodexCredentials::new(
    ///     "token".to_owned(),
    ///     "account".to_owned(),
    /// );
    /// let headers = zeta_agent::codex_request_headers(&credentials, "session");
    /// assert_eq!(headers["Authorization"], "Bearer token");
    /// ```
    pub fn new(access_token: String, account_id: String) -> Self {
        CodexCredentials {
            access_token,
            account_id,
        }
    }
}

/// Builds one streaming Responses request.
///
/// # Examples
///
/// ```
/// use serde_json::json;
/// use zeta_agent::{responses_request, ModelInput};
///
/// let input = ModelInput {
///     messages: vec![json!({"role": "user", "content": "Hello"})
///         .as_object().unwrap().clone()],
///     tools: Vec::new(),
///     tool_choice: json!("auto"),
///     max_tokens: 64,
///     selected_model: Some("unit-model".to_owned()),
///     session_id: Some("session".to_owned()),
///     thinking: None,
/// };
/// let request = responses_request(&input).unwrap();
/// assert_eq!(request["prompt_cache_key"], "session");
/// ```
///
/// # Errors
///
/// Returns [`AgentError`] when the model input has no resolved model name.
pub fn responses_request(input: &ModelInput) -> Result<Map<String, Value>, AgentError> {
    let Some(model) = &input.selected_model else {
        return Err(AgentError::model(
            "model request failed: a codex-responses profile must name its model",
        ));
    };
    let mut body = Map::new();
    body.insert("model".to_owned(), Value::String(model.clone()));
    body.insert("stream".to_owned(), Value::Bool(true));
    body.insert("store".to_owned(), Value::Bool(false));
    body.insert(
        "input".to_owned(),
        Value::Array(responses_input_items(&input.messages)),
    );
    body.insert("include".to_owned(), json!(["reasoning.encrypted_content"]));
    body.insert(
        "reasoning".to_owned(),
        json!({"effort": reasoning_effort(input.thinking.as_deref()), "summary": "auto"}),
    );
    let instructions = responses_instructions(&input.messages);
    if !instructions.is_empty() {
        body.insert("instructions".to_owned(), Value::String(instructions));
    }
    if let Some(session_id) = &input.session_id {
        if !session_id.is_empty() {
            body.insert(
                "prompt_cache_key".to_owned(),
                Value::String(session_id.clone()),
            );
        }
    }
    if !input.tools.is_empty() {
        let mut tools = Vec::new();
        for tool in &input.tools {
            tools.push(responses_tool(tool));
        }
        body.insert("tools".to_owned(), Value::Array(tools));
        let tool_choice = input
            .tool_choice
            .as_str()
            .map(str::to_owned)
            .unwrap_or_else(|| "auto".to_owned());
        body.insert("tool_choice".to_owned(), Value::String(tool_choice));
        body.insert("parallel_tool_calls".to_owned(), Value::Bool(true));
    }
    Ok(body)
}

/// Builds the identity and protocol headers required by Codex Responses.
///
/// # Examples
///
/// ```
/// let credentials = zeta_agent::CodexCredentials::new(
///     "token".to_owned(),
///     "account".to_owned(),
/// );
/// let headers = zeta_agent::codex_request_headers(&credentials, "session");
/// assert_eq!(headers["session-id"], "session");
/// ```
pub fn codex_request_headers(
    credentials: &CodexCredentials,
    session: &str,
) -> BTreeMap<String, String> {
    let mut headers = BTreeMap::new();
    headers.insert("Accept".to_owned(), "text/event-stream".to_owned());
    headers.insert(
        "Authorization".to_owned(),
        format!("Bearer {}", credentials.access_token),
    );
    headers.insert("Content-Type".to_owned(), "application/json".to_owned());
    headers.insert(
        "OpenAI-Beta".to_owned(),
        "responses=experimental".to_owned(),
    );
    headers.insert(
        "chatgpt-account-id".to_owned(),
        credentials.account_id.clone(),
    );
    headers.insert("originator".to_owned(), "zeta".to_owned());
    headers.insert("session-id".to_owned(), session.to_owned());
    headers
}

/// Reconstructs a Responses result from decoded SSE data frames.
///
/// # Examples
///
/// ```
/// let frames = vec![
///     r#"{"type":"response.completed","response":{"status":"completed"}}"#.to_owned(),
///     "[DONE]".to_owned(),
/// ];
/// let decoded = zeta_agent::decode_responses_stream(&frames).unwrap();
/// assert_eq!(decoded.response["choices"][0]["finish_reason"], "stop");
/// ```
///
/// # Errors
///
/// Returns [`AgentError`] when a frame is invalid, the provider reports a
/// failure, or no terminal response event arrives.
pub fn decode_responses_stream(frames: &[String]) -> Result<DecodedModelStream, AgentError> {
    let mut decoder = ResponsesStreamDecoder::default();
    for frame in frames {
        decoder.push_frame(frame)?;
    }
    decoder.finish()
}

#[derive(Default)]
struct ResponsesAccumulator {
    content: Vec<String>,
    reasoning: Vec<String>,
    items: Vec<Value>,
    tool_calls: Vec<Value>,
    usage: Option<Map<String, Value>>,
    status: Option<String>,
    observations: Vec<Observation>,
}

#[derive(Default)]
pub(super) struct ResponsesStreamDecoder {
    accumulator: ResponsesAccumulator,
    done: bool,
}

impl ResponsesStreamDecoder {
    pub(super) fn push_frame(&mut self, frame: &str) -> Result<Vec<Observation>, AgentError> {
        if self.done {
            return Ok(Vec::new());
        }
        let Some(event) = decode_stream_event(frame)? else {
            self.done = true;
            return Ok(Vec::new());
        };
        let observation_count = self.accumulator.observations.len();
        self.accumulator.add_event(&event)?;
        Ok(self.accumulator.observations[observation_count..].to_vec())
    }

    pub(super) fn finish(self) -> Result<DecodedModelStream, AgentError> {
        self.accumulator.response()
    }
}

impl ResponsesAccumulator {
    fn add_event(&mut self, event: &Map<String, Value>) -> Result<(), AgentError> {
        let event_type = event.get("type").and_then(Value::as_str).unwrap_or("");
        if event_type == "error" {
            return Err(AgentError::model(format!(
                "model request failed: {}",
                format_stream_error(&Value::Object(event.clone()))
            )));
        }
        if event_type == "response.failed" {
            return Err(response_failure(event));
        }
        if event_type == "response.reasoning_summary_text.delta"
            || event_type == "response.reasoning_text.delta"
        {
            self.add_reasoning(event.get("delta").and_then(Value::as_str).unwrap_or(""));
        } else if event_type == "response.reasoning_summary_part.done" {
            self.add_reasoning(REASONING_SUMMARY_SEPARATOR);
        } else if event_type == "response.output_text.delta"
            || event_type == "response.refusal.delta"
        {
            self.add_content(event.get("delta").and_then(Value::as_str).unwrap_or(""));
        } else if event_type == "response.output_item.done" {
            self.add_item(event.get("item"));
        } else if event_type == "response.completed"
            || event_type == "response.done"
            || event_type == "response.incomplete"
        {
            self.add_terminal(event_type, event);
        }
        Ok(())
    }

    fn add_reasoning(&mut self, text: &str) {
        if text.is_empty() {
            return;
        }
        self.reasoning.push(text.to_owned());
        self.observations.push(Observation::ReasoningDelta {
            text: text.to_owned(),
        });
    }

    fn add_content(&mut self, text: &str) {
        if text.is_empty() {
            return;
        }
        self.content.push(text.to_owned());
        self.observations.push(Observation::TextDelta {
            text: text.to_owned(),
        });
    }

    fn add_item(&mut self, item: Option<&Value>) {
        let Some(item) = item.and_then(Value::as_object) else {
            return;
        };
        self.items.push(Value::Object(item.clone()));
        if item.get("type").and_then(Value::as_str) != Some("function_call") {
            return;
        }
        self.tool_calls.push(json!({
            "id": item.get("call_id").and_then(Value::as_str).unwrap_or(""),
            "type": "function",
            "function": {
                "name": item.get("name").and_then(Value::as_str).unwrap_or(""),
                "arguments": item.get("arguments").and_then(Value::as_str).unwrap_or(""),
            },
        }));
    }

    fn add_terminal(&mut self, event_type: &str, event: &Map<String, Value>) {
        let response = event.get("response").and_then(Value::as_object);
        let status = response
            .and_then(|response| response.get("status"))
            .and_then(Value::as_str)
            .filter(|status| !status.is_empty())
            .map(str::to_owned)
            .unwrap_or_else(|| {
                if event_type == "response.incomplete" {
                    "incomplete".to_owned()
                } else {
                    "completed".to_owned()
                }
            });
        self.status = Some(status);
        let Some(usage) = response
            .and_then(|response| response.get("usage"))
            .and_then(Value::as_object)
        else {
            return;
        };
        self.usage = Some(responses_usage(usage));
    }

    fn response(self) -> Result<DecodedModelStream, AgentError> {
        let Some(status) = &self.status else {
            return Err(AgentError::model(
                "model stream failed: stream ended before response.completed",
            ));
        };
        let mut content = String::new();
        for item in &self.items {
            let Some(item) = item.as_object() else {
                continue;
            };
            if item.get("type").and_then(Value::as_str) != Some("message") {
                continue;
            }
            let Some(parts) = item.get("content").and_then(Value::as_array) else {
                continue;
            };
            for part in parts {
                let Some(part) = part.as_object() else {
                    continue;
                };
                if part.get("type").and_then(Value::as_str) != Some("output_text") {
                    continue;
                }
                content.push_str(part.get("text").and_then(Value::as_str).unwrap_or(""));
            }
        }
        if content.is_empty() {
            content = self.content.concat();
        }
        let content = if content.is_empty() && !self.tool_calls.is_empty() {
            Value::Null
        } else {
            Value::String(content)
        };
        let mut message = Map::new();
        message.insert("role".to_owned(), Value::String("assistant".to_owned()));
        message.insert("content".to_owned(), content);
        let reasoning = self.reasoning.concat();
        let reasoning = reasoning.trim();
        if !reasoning.is_empty() {
            message.insert(
                "reasoning_content".to_owned(),
                Value::String(reasoning.to_owned()),
            );
        }
        if !self.tool_calls.is_empty() {
            message.insert("tool_calls".to_owned(), Value::Array(self.tool_calls));
        }
        if !self.items.is_empty() {
            message.insert(RESPONSES_ITEMS_FIELD.to_owned(), Value::Array(self.items));
        }
        let finish_reason = if status == "incomplete" {
            "length"
        } else if message.contains_key("tool_calls") {
            "tool_calls"
        } else {
            "stop"
        };
        let mut response = Map::new();
        response.insert(
            "choices".to_owned(),
            json!([{"message": message, "finish_reason": finish_reason}]),
        );
        if let Some(usage) = self.usage {
            response.insert("usage".to_owned(), Value::Object(usage));
        }
        Ok(DecodedModelStream {
            response,
            observations: self.observations,
        })
    }
}

fn responses_instructions(messages: &[Map<String, Value>]) -> String {
    let mut parts = Vec::new();
    for message in messages {
        if message.get("role").and_then(Value::as_str) != Some("system") {
            continue;
        }
        let Some(content) = message.get("content").and_then(Value::as_str) else {
            continue;
        };
        if !content.is_empty() {
            parts.push(content.to_owned());
        }
    }
    parts.join("\n\n")
}

fn responses_input_items(messages: &[Map<String, Value>]) -> Vec<Value> {
    let mut items = Vec::new();
    for message in messages {
        let role = message.get("role").and_then(Value::as_str).unwrap_or("");
        if role == "system" {
            continue;
        }
        if role == "tool" {
            items.push(json!({
                "type": "function_call_output",
                "call_id": message.get("tool_call_id").and_then(Value::as_str).unwrap_or(""),
                "output": message.get("content").and_then(Value::as_str).unwrap_or(""),
            }));
            continue;
        }
        if role == "assistant" {
            append_assistant_items(&mut items, message);
            continue;
        }
        items.push(json!({
            "type": "message",
            "role": if role.is_empty() { "user" } else { role },
            "content": [{
                "type": "input_text",
                "text": message.get("content").and_then(Value::as_str).unwrap_or(""),
            }],
        }));
    }
    items
}

fn append_assistant_items(items: &mut Vec<Value>, message: &Map<String, Value>) {
    if let Some(recorded) = message.get(RESPONSES_ITEMS_FIELD).and_then(Value::as_array) {
        if !recorded.is_empty() {
            for item in recorded {
                if item.is_object() {
                    items.push(item.clone());
                }
            }
            return;
        }
    }
    if let Some(content) = message.get("content").and_then(Value::as_str) {
        if !content.is_empty() {
            items.push(json!({
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": content}],
            }));
        }
    }
    let Some(tool_calls) = message.get("tool_calls").and_then(Value::as_array) else {
        return;
    };
    for call in tool_calls {
        let Some(call) = call.as_object() else {
            continue;
        };
        let function = call.get("function").and_then(Value::as_object);
        items.push(json!({
            "type": "function_call",
            "call_id": call.get("id").and_then(Value::as_str).unwrap_or(""),
            "name": function
                .and_then(|function| function.get("name"))
                .and_then(Value::as_str)
                .unwrap_or(""),
            "arguments": function
                .and_then(|function| function.get("arguments"))
                .and_then(Value::as_str)
                .unwrap_or(""),
        }));
    }
}

fn responses_tool(tool: &Value) -> Value {
    let function = tool
        .as_object()
        .and_then(|tool| tool.get("function"))
        .and_then(Value::as_object);
    json!({
        "type": "function",
        "name": function
            .and_then(|function| function.get("name"))
            .and_then(Value::as_str)
            .unwrap_or(""),
        "description": function
            .and_then(|function| function.get("description"))
            .and_then(Value::as_str)
            .unwrap_or(""),
        "parameters": function
            .and_then(|function| function.get("parameters"))
            .cloned()
            .unwrap_or(Value::Null),
        "strict": Value::Null,
    })
}

fn reasoning_effort(thinking: Option<&str>) -> &'static str {
    if thinking == Some("none") || thinking == Some("minimal") || thinking == Some("low") {
        return "low";
    }
    if thinking == Some("high") {
        return "high";
    }
    "medium"
}

fn responses_usage(usage: &Map<String, Value>) -> Map<String, Value> {
    let fields = [
        ("prompt_tokens", "input_tokens"),
        ("completion_tokens", "output_tokens"),
        ("total_tokens", "total_tokens"),
    ];
    let mut output = Map::new();
    for (target, source) in fields {
        let Some(value) = usage
            .get(source)
            .filter(|value| value.is_i64() || value.is_u64())
        else {
            continue;
        };
        output.insert(target.to_owned(), value.clone());
    }
    output
}

fn response_failure(event: &Map<String, Value>) -> AgentError {
    let error = event
        .get("response")
        .and_then(Value::as_object)
        .and_then(|response| response.get("error"))
        .cloned()
        .unwrap_or_else(|| json!({}));
    AgentError::model(format!(
        "model request failed: {}",
        format_stream_error(&error)
    ))
}
