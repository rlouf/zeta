//! Defines content-addressed values and blob storage for zeta.
//!
//! Exact content bytes use plain BLAKE3. Structured object, derivation, and
//! chain identities use BLAKE3 derive-key mode with one frozen context per
//! domain. Substrate values use the shared canonical JSON representation so
//! independently implemented mints remain byte-identical.

mod domain;
mod hash;
mod objects;
mod store;

pub use domain::{derive, Domain};
pub use hash::{hash_bytes, hash_file, Hash, HashParseError};
pub use objects::{
    canonical_json, CanonicalJsonError, Derivation, Object, ObjectId, Ref, RefName, RefUpdate,
};
pub use store::BlobStore;
