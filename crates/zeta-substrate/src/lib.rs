//! Content addresses and derived identifiers for the zeta ecosystem.
//!
//! Two address kinds exist, and the split is normative
//! (`spec/wire-v0.md` §11). Content addresses hash exact bytes with
//! plain, undomained BLAKE3. This keeps one hash universe real: a
//! stagefs pre-image hash, a pack blob name, and an event's
//! `payload_hash` are the same string for the same bytes, with no
//! shared code between the tools. Derived identifiers hash structured
//! identity with BLAKE3 derive-key mode and one frozen context string
//! per domain, because namespace confusion is the real risk there.
//!
//! The golden vectors in `spec/vectors/addresses/vectors.json` and
//! the pinned digests in this crate's tests are the drift tripwires:
//! a dependency bump that changes any output fails loudly.

mod domain;
mod hash;
mod objects;
mod store;

pub use domain::{derive, Domain};
pub use hash::{hash_bytes, hash_file, Hash, HashParseError};
pub use objects::{parse_id, Id};
pub use store::{BlobStore, Layout};
