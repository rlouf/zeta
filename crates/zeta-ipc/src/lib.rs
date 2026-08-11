//! Sans-IO implementation of the wire-v0 plugin protocol.
//!
//! The crate holds pure functions and state machines: envelopes with
//! validation (`spec/wire-v0.md`), canonical JSON, a per-side
//! [`Session`] machine fed with parsed envelopes and clock instants,
//! and a thin blocking line framer over [`Read`]/[`Write`]. No
//! sockets, no async runtime. Sans-IO keeps conformance testing
//! trivial — the golden vectors drive the same code paths production
//! IO does — and it leaves transport choices to callers. A tokio
//! adapter is deferred to Phase 3 on purpose.
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
