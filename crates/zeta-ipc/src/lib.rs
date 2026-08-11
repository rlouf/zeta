//! Sans-IO implementation of the wire-v0 plugin protocol.
//!
//! The crate provides validated envelopes (`spec/wire-v0.md`), canonical JSON,
//! per-side [`Session`] machines fed with parsed envelopes and clock instants,
//! and blocking line framing over [`Read`]/[`Write`]. Sans-IO lets golden
//! vectors exercise the same state-machine paths as live IO and leaves
//! transport choices to callers.
//!
//! [`Read`]: std::io::Read
//! [`Write`]: std::io::Write
//! [`Session`]: crate::session

mod canonical;
mod envelope;
mod error;
mod frame;
pub mod session;
mod timestamp;
mod validate;

pub use canonical::canonical_json;
pub use envelope::{
    Ack, Call, CallInfo, CallResult, Common, Envelope, ErrorEnvelope, ErrorInfo, EventEnvelope,
    EventTypeDecl, Heartbeat, Hello, HelloAck, Kind, OperationDecl, Shutdown,
    MAX_INLINE_PAYLOAD_BYTES, PROTOCOL_VERSION,
};
pub use error::WireError;
pub use frame::{Frame, FrameReader, FrameWriter, Violation, MAX_FRAME_BYTES};
pub use validate::validate_envelope;
