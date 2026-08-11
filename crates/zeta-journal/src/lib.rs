//! Backend-neutral event journal semantics for the zeta ecosystem.
//!
//! The module defines event values, canonical payload and chain identities,
//! verification, and an in-memory conformance reference.

mod chain;
mod error;
mod event;
mod memory;

pub use chain::{
    canonical_chain_bytes, canonical_payload, entry_address, payload_address, verify,
    HeadExpectation, JournalEntry,
};
pub use error::{AppendError, VerificationError, VerificationErrorKind, VerificationReport};
pub use event::{AppendOutcome, DraftEvent, Event, Filter};
pub use memory::MemoryJournal;
