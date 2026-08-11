//! Blocking ndjson framing over `Read`/`Write` (spec §2).
//!
//! The reader owns its buffer and never panics on peer garbage:
//! every line comes back as either a validated envelope or a
//! [`Violation`] the caller decides about. Overlong lines discard to
//! the next newline instead of growing the buffer without bound,
//! because a misbehaving peer must not exhaust the supervisor's
//! memory. This is the whole IO surface of the crate; the async
//! adapter is deferred to Phase 3.

use std::io::{Read, Write};

use crate::envelope::Envelope;
use crate::error::WireError;

/// The frame-size ceiling, matching the Python implementation.
pub const MAX_FRAME_BYTES: usize = 8 * 1024 * 1024;

const READ_CHUNK: usize = 64 * 1024;
const PREVIEW_BYTES: usize = 200;

/// One line read from the peer.
#[derive(Clone, Debug, PartialEq)]
pub enum Frame {
    /// A validated envelope.
    Envelope(Envelope),
    /// A line that was not a valid envelope.
    Violation(Violation),
}

/// One stream line that was not a valid envelope.
#[derive(Clone, Debug, PartialEq)]
pub struct Violation {
    /// The violated rule token (`bad_json`, `empty_line`,
    /// `frame_too_long`, or an envelope rule).
    pub rule: String,
    /// A human-readable description.
    pub detail: String,
    /// A bounded, lossy preview of the offending line.
    pub preview: String,
}

/// A buffered line reader that yields envelopes and violations.
///
/// # Examples
///
/// ```
/// use zeta_ipc::{Frame, FrameReader};
///
/// let lines = b"junk\n{\"id\":\"m-1\",\"kind\":\"heartbeat\",\"ts\":\"2026-08-10T12:00:00Z\",\"v\":0}\n";
/// let mut reader = FrameReader::new(&lines[..]);
/// let Some(Frame::Violation(violation)) = reader.read_frame().unwrap() else {
///     panic!("junk must surface as a violation");
/// };
/// assert_eq!(violation.rule, "bad_json");
/// let Some(Frame::Envelope(_)) = reader.read_frame().unwrap() else {
///     panic!("the envelope must parse");
/// };
/// assert_eq!(reader.read_frame().unwrap(), None);
/// ```
pub struct FrameReader<R: Read> {
    inner: R,
    buffer: Vec<u8>,
    eof: bool,
    max_frame_bytes: usize,
}

impl<R: Read> FrameReader<R> {
    /// Creates a reader with the default frame ceiling.
    pub fn new(inner: R) -> Self {
        FrameReader {
            inner,
            buffer: Vec::new(),
            eof: false,
            max_frame_bytes: MAX_FRAME_BYTES,
        }
    }

    /// Creates a reader with an explicit frame ceiling, for tests.
    pub fn with_max_frame_bytes(inner: R, max_frame_bytes: usize) -> Self {
        FrameReader {
            inner,
            buffer: Vec::new(),
            eof: false,
            max_frame_bytes,
        }
    }

    /// Reads one frame; `None` at end-of-stream.
    ///
    /// # Errors
    ///
    /// Returns [`io::Error`] only for transport failures; peer
    /// garbage surfaces as [`Frame::Violation`], never as an error.
    ///
    /// [`io::Error`]: std::io::Error
    pub fn read_frame(&mut self) -> std::io::Result<Option<Frame>> {
        loop {
            let newline = position_of_newline(&self.buffer);
            if let Some(newline) = newline {
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
            let mut chunk = [0u8; READ_CHUNK];
            let count = self.inner.read(&mut chunk)?;
            if count == 0 {
                self.eof = true;
            } else {
                self.buffer.extend_from_slice(&chunk[..count]);
            }
        }
    }

    fn discard_until_newline(&mut self) -> std::io::Result<Option<Frame>> {
        let head: Vec<u8> = self.buffer.iter().take(PREVIEW_BYTES).copied().collect();
        self.buffer.clear();
        loop {
            let mut chunk = [0u8; READ_CHUNK];
            let count = self.inner.read(&mut chunk)?;
            if count == 0 {
                self.eof = true;
                break;
            }
            let newline = position_of_newline(&chunk[..count]);
            if let Some(newline) = newline {
                self.buffer.extend_from_slice(&chunk[newline + 1..count]);
                break;
            }
        }
        Ok(Some(overlong_frame(&head, self.max_frame_bytes)))
    }
}

fn position_of_newline(bytes: &[u8]) -> Option<usize> {
    let mut position = None;
    for (index, byte) in bytes.iter().enumerate() {
        if *byte == b'\n' {
            position = Some(index);
            break;
        }
    }
    position
}

fn overlong_frame(line: &[u8], max_frame_bytes: usize) -> Frame {
    Frame::Violation(Violation {
        rule: "frame_too_long".to_string(),
        detail: format!("line exceeded the {max_frame_bytes}-byte frame limit"),
        preview: preview(line),
    })
}

fn decode_line(line: &[u8]) -> Frame {
    let mut trimmed = line;
    while let [head @ .., b'\r'] = trimmed {
        trimmed = head;
    }
    if trimmed.is_empty() {
        return Frame::Violation(Violation {
            rule: "empty_line".to_string(),
            detail: "empty line".to_string(),
            preview: String::new(),
        });
    }
    let text = String::from_utf8_lossy(trimmed);
    let envelope = Envelope::parse_str(&text);
    match envelope {
        Ok(envelope) => Frame::Envelope(envelope),
        Err(WireError { rule, message }) => Frame::Violation(Violation {
            rule,
            detail: message,
            preview: preview(trimmed),
        }),
    }
}

fn preview(line: &[u8]) -> String {
    let text = String::from_utf8_lossy(line);
    let mut preview = String::new();
    for (index, character) in text.chars().enumerate() {
        if index >= PREVIEW_BYTES {
            preview.push('…');
            break;
        }
        preview.push(character);
    }
    preview
}

/// A writer that frames envelopes as canonical lines.
pub struct FrameWriter<W: Write> {
    inner: W,
}

impl<W: Write> FrameWriter<W> {
    /// Creates a writer over any byte sink.
    pub fn new(inner: W) -> Self {
        FrameWriter { inner }
    }

    /// Writes one envelope as a canonical JSON line and flushes.
    ///
    /// Flushing per frame keeps latency bounded: a protocol message
    /// held in a buffer is indistinguishable from a dead peer.
    ///
    /// # Errors
    ///
    /// Returns [`io::Error`] if the transport fails.
    ///
    /// [`io::Error`]: std::io::Error
    pub fn write_envelope(&mut self, envelope: &Envelope) -> std::io::Result<()> {
        let line = envelope.to_canonical_json();
        self.inner.write_all(line.as_bytes())?;
        self.inner.write_all(b"\n")?;
        self.inner.flush()
    }
}
