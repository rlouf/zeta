//! Decodes server-sent event framing without owning a network client.

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};

use crate::error::AgentError;

/// Incrementally decodes bounded SSE frames from arbitrary byte chunks.
pub struct SseByteDecoder {
    line: Vec<u8>,
    data: Vec<String>,
    event_bytes: usize,
    max_event_bytes: usize,
}

impl SseByteDecoder {
    /// Creates a decoder with one maximum event size.
    ///
    /// # Examples
    ///
    /// ```
    /// let mut decoder = zeta_agent::SseByteDecoder::new(1024);
    /// assert!(decoder.push(b"data: hel").unwrap().is_empty());
    /// assert_eq!(decoder.push(b"lo\n\n").unwrap(), ["hello"]);
    /// ```
    pub fn new(max_event_bytes: usize) -> Self {
        SseByteDecoder {
            line: Vec::new(),
            data: Vec::new(),
            event_bytes: 0,
            max_event_bytes,
        }
    }

    /// Accepts one byte chunk and returns every complete data frame.
    ///
    /// # Errors
    ///
    /// Returns [`AgentError`] when one line is not UTF-8 or an event exceeds
    /// the configured bound.
    pub fn push(&mut self, chunk: &[u8]) -> Result<Vec<String>, AgentError> {
        let mut frames = Vec::new();
        for byte in chunk {
            if *byte == b'\n' {
                self.finish_line(&mut frames)?;
                continue;
            }
            self.line.push(*byte);
            if self.line.len() > self.max_event_bytes {
                return Err(self.too_large());
            }
        }
        Ok(frames)
    }

    /// Flushes a final unterminated line and any pending data frame.
    ///
    /// # Errors
    ///
    /// Returns [`AgentError`] when the final line is not UTF-8 or the event
    /// exceeds the configured bound.
    pub fn finish(mut self) -> Result<Vec<String>, AgentError> {
        let mut frames = Vec::new();
        if !self.line.is_empty() {
            self.finish_line(&mut frames)?;
        }
        self.push_frame(&mut frames);
        Ok(frames)
    }

    fn finish_line(&mut self, frames: &mut Vec<String>) -> Result<(), AgentError> {
        if self.line.last() == Some(&b'\r') {
            self.line.pop();
        }
        let line = std::mem::take(&mut self.line);
        let line = String::from_utf8(line)
            .map_err(|_| AgentError::model("model stream failed: SSE data was not UTF-8"))?;
        if line.is_empty() {
            self.push_frame(frames);
            return Ok(());
        }
        if line.starts_with(':') {
            return Ok(());
        }
        let Some(value) = line.strip_prefix("data:") else {
            return Ok(());
        };
        let value = value.strip_prefix(' ').unwrap_or(value);
        let separator_bytes = usize::from(!self.data.is_empty());
        self.event_bytes += separator_bytes + value.len();
        if self.event_bytes > self.max_event_bytes {
            return Err(self.too_large());
        }
        self.data.push(value.to_owned());
        Ok(())
    }

    fn push_frame(&mut self, frames: &mut Vec<String>) {
        if self.data.is_empty() {
            return;
        }
        frames.push(self.data.join("\n"));
        self.data.clear();
        self.event_bytes = 0;
    }

    fn too_large(&self) -> AgentError {
        AgentError::model(format!(
            "model stream failed: SSE event exceeded {} bytes",
            self.max_event_bytes
        ))
    }
}

/// Maps stream timing intent onto explicit transport timeout fields.
#[derive(Clone, Copy, Debug, Deserialize, PartialEq, Serialize)]
pub struct ModelStreamTimeout {
    /// Bounds connection establishment and time to first output.
    pub connect: f64,
    /// Bounds connection-pool acquisition and time to first output.
    pub pool: f64,
    /// Bounds the delay between streamed output frames.
    pub read: f64,
    /// Bounds request-body transmission and time to first output.
    pub write: f64,
}

/// Returns explicit timeout fields for one streamed model request.
///
/// # Examples
///
/// ```
/// let timeout = zeta_agent::model_stream_timeout(10.0, 2.5);
/// assert_eq!(timeout.connect, 10.0);
/// assert_eq!(timeout.read, 2.5);
/// ```
pub fn model_stream_timeout(first_output_seconds: f64, idle_seconds: f64) -> ModelStreamTimeout {
    ModelStreamTimeout {
        connect: first_output_seconds,
        pool: first_output_seconds,
        read: idle_seconds,
        write: first_output_seconds,
    }
}

/// Collects SSE data frames from already decoded transport lines.
///
/// # Examples
///
/// ```
/// let lines = vec!["data: first".to_owned(), String::new()];
/// assert_eq!(zeta_agent::parse_sse_lines(&lines), ["first"]);
/// ```
pub fn parse_sse_lines(lines: &[String]) -> Vec<String> {
    let mut frames = Vec::new();
    let mut data = Vec::new();
    for line in lines {
        if line.is_empty() {
            push_frame(&mut frames, &mut data);
            continue;
        }
        if line.starts_with(':') {
            continue;
        }
        let Some(value) = line.strip_prefix("data:") else {
            continue;
        };
        data.push(value.strip_prefix(' ').unwrap_or(value).to_owned());
    }
    push_frame(&mut frames, &mut data);
    frames
}

pub(super) fn decode_stream_event(data: &str) -> Result<Option<Map<String, Value>>, AgentError> {
    if data == "[DONE]" {
        return Ok(None);
    }
    let value = serde_json::from_str::<Value>(data).map_err(|error| {
        AgentError::model(format!(
            "model stream failed: invalid JSON event: {}",
            json_error_detail(data, &error)
        ))
    })?;
    let Value::Object(event) = value else {
        return Err(AgentError::model(
            "model stream failed: event was not a JSON object",
        ));
    };
    Ok(Some(event))
}

pub(super) fn format_stream_error(error: &Value) -> String {
    let Some(error) = error.as_object() else {
        if let Some(error) = error.as_str() {
            return error.to_owned();
        }
        return serde_json::to_string(error).unwrap_or_else(|_| "null".to_owned());
    };
    let Some(message) = error.get("message").and_then(Value::as_str) else {
        return serde_json::to_string(error).unwrap_or_else(|_| "{}".to_owned());
    };
    message.to_owned()
}

fn push_frame(frames: &mut Vec<String>, data: &mut Vec<String>) {
    if data.is_empty() {
        return;
    }
    frames.push(data.join("\n"));
    data.clear();
}

fn json_error_detail(data: &str, error: &serde_json::Error) -> String {
    if error.is_eof() {
        let char_index = data.chars().count();
        let mut line = 1;
        let mut column = 1;
        for character in data.chars() {
            if character == '\n' {
                line += 1;
                column = 1;
            } else {
                column += 1;
            }
        }
        return format!("Expecting value: line {line} column {column} (char {char_index})");
    }
    error.to_string()
}
