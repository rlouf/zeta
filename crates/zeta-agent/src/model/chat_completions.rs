//! Translates chat-completions requests and streamed response events.

use std::collections::BTreeMap;

use serde_json::{Map, Value, json};

use super::sse::{decode_stream_event, format_stream_error};
use super::{DecodedModelStream, ModelInput, Observation};
use crate::error::AgentError;

const METADATA_FIELDS: [&str; 5] = ["id", "object", "created", "model", "system_fingerprint"];
const USAGE_FIELDS: [&str; 3] = ["prompt_tokens", "completion_tokens", "total_tokens"];

/// Builds one OpenAI-compatible chat-completions request.
///
/// # Examples
///
/// ```
/// use serde_json::{json, Map};
/// use zeta_agent::{chat_completions_request, ModelInput};
///
/// let input = ModelInput {
///     messages: vec![json!({"role": "user", "content": "Hello"})
///         .as_object().unwrap().clone()],
///     tools: Vec::new(),
///     tool_choice: json!("auto"),
///     max_tokens: 64,
///     selected_model: Some("unit-model".to_owned()),
///     session_id: None,
///     thinking: None,
/// };
/// let request: Map<String, serde_json::Value> = chat_completions_request(&input).unwrap();
/// assert_eq!(request["model"], "unit-model");
/// ```
///
/// # Errors
///
/// Returns [`AgentError`] when the model input has no resolved model name.
pub fn chat_completions_request(input: &ModelInput) -> Result<Map<String, Value>, AgentError> {
    let Some(model) = &input.selected_model else {
        return Err(AgentError::model(
            "model request failed: a chat-completions profile must name its model",
        ));
    };
    let mut body = Map::new();
    body.insert("model".to_owned(), Value::String(model.clone()));
    let mut messages = Vec::new();
    for message in &input.messages {
        messages.push(Value::Object(message.clone()));
    }
    body.insert("messages".to_owned(), Value::Array(messages));
    body.insert("temperature".to_owned(), json!(0.2));
    body.insert("max_tokens".to_owned(), json!(input.max_tokens));
    body.insert("stream_options".to_owned(), json!({"include_usage": true}));
    if input.thinking.as_deref() == Some("none") {
        body.insert(
            "chat_template_kwargs".to_owned(),
            json!({"enable_thinking": false}),
        );
    } else if let Some(thinking) = &input.thinking {
        body.insert(
            "reasoning_effort".to_owned(),
            Value::String(thinking.clone()),
        );
    }
    if !input.tools.is_empty() {
        body.insert("tools".to_owned(), Value::Array(input.tools.clone()));
        body.insert("tool_choice".to_owned(), input.tool_choice.clone());
    }
    Ok(body)
}

/// Reconstructs a chat-completions response from decoded SSE data frames.
///
/// # Examples
///
/// ```
/// let frames = vec![
///     r#"{"choices":[{"index":0,"delta":{"content":"done"},"finish_reason":"stop"}]}"#.to_owned(),
///     "[DONE]".to_owned(),
/// ];
/// let decoded = zeta_agent::decode_chat_completions_stream(&frames).unwrap();
/// assert_eq!(decoded.response["choices"][0]["message"]["content"], "done");
/// ```
///
/// # Errors
///
/// Returns [`AgentError`] when a frame is invalid, the provider reports an
/// error, the completion contains invalid protocol values, or `[DONE]` is
/// missing.
pub fn decode_chat_completions_stream(frames: &[String]) -> Result<DecodedModelStream, AgentError> {
    let mut decoder = ChatStreamDecoder::default();
    for frame in frames {
        decoder.push_frame(frame)?;
    }
    decoder.finish()
}

/// Formats one HTTP status failure with a bounded provider detail.
///
/// # Examples
///
/// ```
/// let body = serde_json::json!({"error": {"message": "busy"}});
/// let detail = zeta_agent::http_error_detail(429, "https://model.invalid", &body);
/// assert!(detail.ends_with(": busy"));
/// ```
pub fn http_error_detail(status: u16, url: &str, body: &Value) -> String {
    let category = if (400..500).contains(&status) {
        "Client error"
    } else if (500..600).contains(&status) {
        "Server error"
    } else if (300..400).contains(&status) {
        "Redirect response"
    } else {
        "Invalid status code"
    };
    let reason = status_reason(status);
    let detail = body
        .get("error")
        .map(format_stream_error)
        .unwrap_or_else(|| format_stream_error(body));
    format!(
        "{category} '{status} {reason}' for url '{url}'\nFor more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/{status}: {detail}"
    )
}

#[derive(Default)]
struct ChatAccumulator {
    metadata: Map<String, Value>,
    role: Option<String>,
    content: Vec<String>,
    reasoning: Vec<String>,
    tool_calls: BTreeMap<u64, ToolCall>,
    finish_reason: Value,
    usage: Option<Map<String, Value>>,
    seen_choice: bool,
    observations: Vec<Observation>,
}

#[derive(Default)]
pub(super) struct ChatStreamDecoder {
    accumulator: ChatAccumulator,
    done: bool,
}

impl ChatStreamDecoder {
    pub(super) fn push_frame(&mut self, frame: &str) -> Result<Vec<Observation>, AgentError> {
        if self.done {
            return Ok(Vec::new());
        }
        let Some(chunk) = decode_stream_event(frame)? else {
            self.done = true;
            return Ok(Vec::new());
        };
        if let Some(error) = chunk.get("error") {
            return Err(AgentError::model(format!(
                "model request failed: {}",
                format_stream_error(error)
            )));
        }
        let observation_count = self.accumulator.observations.len();
        self.accumulator.add_chunk(&chunk)?;
        Ok(self.accumulator.observations[observation_count..].to_vec())
    }

    pub(super) fn finish(self) -> Result<DecodedModelStream, AgentError> {
        if !self.done {
            return Err(AgentError::model(
                "model stream failed: stream ended before [DONE]",
            ));
        }
        self.accumulator.response()
    }
}

impl ChatAccumulator {
    fn add_chunk(&mut self, chunk: &Map<String, Value>) -> Result<(), AgentError> {
        for key in METADATA_FIELDS {
            if self.metadata.contains_key(key) {
                continue;
            }
            if let Some(value) = chunk.get(key).filter(|value| !value.is_null()) {
                self.metadata.insert(key.to_owned(), value.clone());
            }
        }
        if let Some(usage) = chunk.get("usage").and_then(normalized_usage) {
            self.usage = Some(usage);
        }
        let Some(choices) = chunk.get("choices") else {
            if self.usage.is_some() {
                return Ok(());
            }
            return Err(AgentError::model(
                "model stream failed: event choices were invalid",
            ));
        };
        let Some(choices) = choices.as_array() else {
            return Err(AgentError::model(
                "model stream failed: event choices were invalid",
            ));
        };
        for choice in choices {
            let Some(choice) = choice.as_object() else {
                return Err(AgentError::model(
                    "model stream failed: event choice was invalid",
                ));
            };
            if choice.get("index").and_then(Value::as_u64).unwrap_or(0) != 0 {
                continue;
            }
            self.seen_choice = true;
            if let Some(reason) = choice.get("finish_reason").filter(|value| !value.is_null()) {
                self.finish_reason = reason.clone();
            }
            let delta = choice.get("delta").cloned().unwrap_or_else(|| json!({}));
            let Some(delta) = delta.as_object() else {
                return Err(AgentError::model(
                    "model stream failed: event delta was invalid",
                ));
            };
            self.add_delta(delta)?;
        }
        Ok(())
    }

    fn add_delta(&mut self, delta: &Map<String, Value>) -> Result<(), AgentError> {
        if let Some(role) = delta.get("role").and_then(Value::as_str) {
            self.role = Some(role.to_owned());
        }
        if let Some(content) = delta.get("content").and_then(Value::as_str) {
            if !content.is_empty() {
                self.content.push(content.to_owned());
                self.observations.push(Observation::TextDelta {
                    text: content.to_owned(),
                });
            }
        }
        if let Some(reasoning) = delta.get("reasoning_content").and_then(Value::as_str) {
            if !reasoning.is_empty() {
                self.reasoning.push(reasoning.to_owned());
                self.observations.push(Observation::ReasoningDelta {
                    text: reasoning.to_owned(),
                });
            }
        }
        if let Some(tool_calls) = delta.get("tool_calls") {
            self.add_tool_calls(tool_calls)?;
        }
        Ok(())
    }

    fn add_tool_calls(&mut self, tool_calls: &Value) -> Result<(), AgentError> {
        let Some(tool_calls) = tool_calls.as_array() else {
            return Err(AgentError::model(
                "model stream failed: tool call delta was invalid",
            ));
        };
        for tool_call in tool_calls {
            let Some(tool_call) = tool_call.as_object() else {
                return Err(AgentError::model(
                    "model stream failed: tool call was invalid",
                ));
            };
            let Some(index) = tool_call.get("index").and_then(Value::as_u64) else {
                return Err(AgentError::model(
                    "model stream failed: tool call index was invalid",
                ));
            };
            let call = self.tool_calls.entry(index).or_default();
            if let Some(id) = tool_call.get("id").and_then(Value::as_str) {
                call.id = Some(id.to_owned());
            }
            if let Some(kind) = tool_call.get("type").and_then(Value::as_str) {
                call.kind = Some(kind.to_owned());
            }
            let Some(function) = tool_call.get("function") else {
                continue;
            };
            let Some(function) = function.as_object() else {
                return Err(AgentError::model(
                    "model stream failed: tool function was invalid",
                ));
            };
            if let Some(name) = function.get("name").and_then(Value::as_str) {
                call.name.push_str(name);
            }
            if let Some(arguments) = function.get("arguments").and_then(Value::as_str) {
                call.arguments.push_str(arguments);
            }
        }
        Ok(())
    }

    fn response(self) -> Result<DecodedModelStream, AgentError> {
        if !self.seen_choice {
            return Err(AgentError::model(
                "model stream failed: no completion choices received",
            ));
        }
        let mut message = Map::new();
        message.insert(
            "role".to_owned(),
            Value::String(self.role.unwrap_or_else(|| "assistant".to_owned())),
        );
        message.insert("content".to_owned(), Value::String(self.content.concat()));
        if !self.reasoning.is_empty() {
            message.insert(
                "reasoning_content".to_owned(),
                Value::String(self.reasoning.concat()),
            );
        }
        if !self.tool_calls.is_empty() {
            let mut tool_calls = Vec::new();
            for (index, call) in self.tool_calls {
                tool_calls.push(call.into_value(index));
            }
            message.insert("tool_calls".to_owned(), Value::Array(tool_calls));
        }
        let choice = json!({
            "index": 0,
            "message": message,
            "finish_reason": self.finish_reason,
        });
        let mut response = self.metadata;
        if let Some(usage) = self.usage {
            response.insert("usage".to_owned(), Value::Object(usage));
        }
        response.insert("choices".to_owned(), Value::Array(vec![choice]));
        Ok(DecodedModelStream {
            response,
            observations: self.observations,
        })
    }
}

#[derive(Default)]
struct ToolCall {
    id: Option<String>,
    kind: Option<String>,
    name: String,
    arguments: String,
}

impl ToolCall {
    fn into_value(self, index: u64) -> Value {
        json!({
            "id": self.id.unwrap_or_else(|| format!("call-{index}")),
            "type": self.kind.unwrap_or_else(|| "function".to_owned()),
            "function": {
                "name": self.name,
                "arguments": self.arguments,
            },
        })
    }
}

fn normalized_usage(value: &Value) -> Option<Map<String, Value>> {
    let value = value.as_object()?;
    let mut usage = Map::new();
    for key in USAGE_FIELDS {
        let Some(tokens) = value
            .get(key)
            .filter(|tokens| tokens.is_i64() || tokens.is_u64())
        else {
            continue;
        };
        usage.insert(key.to_owned(), tokens.clone());
    }
    if usage.is_empty() {
        return None;
    }
    Some(usage)
}

fn status_reason(status: u16) -> &'static str {
    if status == 400 {
        return "Bad Request";
    }
    if status == 401 {
        return "Unauthorized";
    }
    if status == 403 {
        return "Forbidden";
    }
    if status == 404 {
        return "Not Found";
    }
    if status == 408 {
        return "Request Timeout";
    }
    if status == 409 {
        return "Conflict";
    }
    if status == 422 {
        return "Unprocessable Entity";
    }
    if status == 429 {
        return "Too Many Requests";
    }
    if status == 500 {
        return "Internal Server Error";
    }
    if status == 502 {
        return "Bad Gateway";
    }
    if status == 503 {
        return "Service Unavailable";
    }
    if status == 504 {
        return "Gateway Timeout";
    }
    "Unknown Status"
}
