//! Blocking NDJSON framing over [`Read`] and [`Write`].
//!
//! [`Read`]: std::io::Read
//! [`Write`]: std::io::Write

use std::io::{Read, Write};

use serde_json::Value;

use crate::error::{INVALID_REQUEST, PARSE_ERROR};
use crate::message::{Message, RequestId};

/// Contains the maximum size of one JSON line.
pub const MAX_FRAME_BYTES: usize = 8 * 1024 * 1024;

const READ_CHUNK: usize = 64 * 1024;
const PREVIEW_CHARACTERS: usize = 200;

/// Contains one decoded message or bounded framing violation.
#[derive(Clone, Debug, PartialEq)]
pub enum Frame {
    /// Contains a valid JSON-RPC message.
    Message(Message),
    /// Contains an invalid input line.
    Violation(Violation),
}

/// Describes one input line that did not contain a valid message.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Violation {
    /// Contains a stable framing rule name.
    pub rule: String,
    /// Contains the corresponding JSON-RPC error code.
    pub code: i64,
    /// Contains a human-readable description.
    pub detail: String,
    /// Contains the valid identifier recovered from an invalid request.
    pub request_id: Option<RequestId>,
    /// Contains a bounded, lossy preview of the line.
    pub preview: String,
}

/// Reads bounded newline-delimited JSON-RPC messages.
pub struct FrameReader<R: Read> {
    inner: R,
    buffer: Vec<u8>,
    eof: bool,
    max_frame_bytes: usize,
}

impl<R: Read> FrameReader<R> {
    /// Creates a reader with the protocol frame limit.
    pub fn new(inner: R) -> Self {
        Self {
            inner,
            buffer: Vec::new(),
            eof: false,
            max_frame_bytes: MAX_FRAME_BYTES,
        }
    }

    /// Creates a reader with an explicit frame limit.
    ///
    /// This constructor supports bounded tests and constrained transports.
    pub fn with_max_frame_bytes(inner: R, max_frame_bytes: usize) -> Self {
        Self {
            inner,
            buffer: Vec::new(),
            eof: false,
            max_frame_bytes,
        }
    }

    /// Reads one frame and accepts a complete final object at EOF.
    ///
    /// # Errors
    ///
    /// Returns [`std::io::Error`] only when the underlying reader fails.
    pub fn read_frame(&mut self) -> std::io::Result<Option<Frame>> {
        loop {
            if let Some(newline) = position_of_newline(&self.buffer) {
                let mut line: Vec<u8> = self.buffer.drain(..=newline).collect();
                line.pop();
                if line.len() > self.max_frame_bytes {
                    return Ok(Some(overlong_frame(&line, self.max_frame_bytes)));
                }
                return Ok(Some(decode_line(&line)));
            }
            if self.eof {
                if self.buffer.is_empty() {
                    return Ok(None);
                }
                let line = std::mem::take(&mut self.buffer);
                if line.len() > self.max_frame_bytes {
                    return Ok(Some(overlong_frame(&line, self.max_frame_bytes)));
                }
                return Ok(Some(decode_line(&line)));
            }
            if self.buffer.len() > self.max_frame_bytes {
                return self.discard_until_newline();
            }
            let mut chunk = [0_u8; READ_CHUNK];
            let count = self.inner.read(&mut chunk)?;
            if count == 0 {
                self.eof = true;
            } else {
                self.buffer.extend_from_slice(&chunk[..count]);
            }
        }
    }

    fn discard_until_newline(&mut self) -> std::io::Result<Option<Frame>> {
        let preview_length = self.buffer.len().min(PREVIEW_CHARACTERS);
        let mut head = Vec::with_capacity(preview_length);
        for byte in &self.buffer[..preview_length] {
            head.push(*byte);
        }
        self.buffer.clear();
        loop {
            let mut chunk = [0_u8; READ_CHUNK];
            let count = self.inner.read(&mut chunk)?;
            if count == 0 {
                self.eof = true;
                break;
            }
            let Some(newline) = position_of_newline(&chunk[..count]) else {
                continue;
            };
            self.buffer.extend_from_slice(&chunk[newline + 1..count]);
            break;
        }
        Ok(Some(overlong_frame(&head, self.max_frame_bytes)))
    }
}

fn position_of_newline(bytes: &[u8]) -> Option<usize> {
    for (index, byte) in bytes.iter().enumerate() {
        if *byte == b'\n' {
            return Some(index);
        }
    }
    None
}

fn overlong_frame(line: &[u8], max_frame_bytes: usize) -> Frame {
    Frame::Violation(Violation {
        rule: "frame_too_long".to_string(),
        code: PARSE_ERROR,
        detail: format!("line exceeded the {max_frame_bytes}-byte frame limit"),
        request_id: None,
        preview: preview(line),
    })
}

fn decode_line(line: &[u8]) -> Frame {
    let mut line = line;
    while let [head @ .., b'\r'] = line {
        line = head;
    }
    if line.is_empty() {
        return Frame::Violation(Violation {
            rule: "empty_line".to_string(),
            code: PARSE_ERROR,
            detail: "empty line".to_string(),
            request_id: None,
            preview: String::new(),
        });
    }
    let Ok(text) = std::str::from_utf8(line) else {
        return Frame::Violation(Violation {
            rule: "parse_error".to_string(),
            code: PARSE_ERROR,
            detail: "the line is not valid UTF-8".to_string(),
            request_id: None,
            preview: preview(line),
        });
    };
    let Ok(value) = serde_json::from_str::<Value>(text) else {
        return Frame::Violation(Violation {
            rule: "parse_error".to_string(),
            code: PARSE_ERROR,
            detail: "the line is not valid JSON".to_string(),
            request_id: None,
            preview: preview(line),
        });
    };
    match Message::parse_value(&value) {
        Ok(message) => Frame::Message(message),
        Err(error) => {
            let rule = if error.code == PARSE_ERROR {
                "parse_error"
            } else if error.code == INVALID_REQUEST {
                "invalid_request"
            } else {
                "invalid_params"
            };
            Frame::Violation(Violation {
                rule: rule.to_string(),
                code: error.code,
                detail: error.message,
                request_id: recover_request_id(&value),
                preview: preview(line),
            })
        }
    }
}

fn recover_request_id(value: &Value) -> Option<RequestId> {
    let fields = value.as_object()?;
    if !fields.contains_key("method")
        || fields.contains_key("result")
        || fields.contains_key("error")
    {
        return None;
    }
    RequestId::parse(fields.get("id")?).ok()
}

fn preview(line: &[u8]) -> String {
    let text = String::from_utf8_lossy(line);
    let mut preview = String::new();
    for (index, character) in text.chars().enumerate() {
        if index >= PREVIEW_CHARACTERS {
            preview.push('…');
            break;
        }
        preview.push(character);
    }
    preview
}

/// Writes compact newline-delimited JSON-RPC messages.
pub struct FrameWriter<W: Write> {
    inner: W,
}

impl<W: Write> FrameWriter<W> {
    /// Creates a writer over a byte sink.
    pub fn new(inner: W) -> Self {
        Self { inner }
    }

    /// Writes one compact message, a newline, and flushes the sink.
    ///
    /// # Errors
    ///
    /// Returns [`std::io::Error`] when the underlying writer fails.
    pub fn write_message(&mut self, message: &Message) -> std::io::Result<()> {
        let line = message.to_json();
        self.inner.write_all(line.as_bytes())?;
        self.inner.write_all(b"\n")?;
        self.inner.flush()
    }
}
