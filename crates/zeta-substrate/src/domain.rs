//! Derived-identifier domains and their frozen derive-key contexts.
//!
//! Objects and derivations separate their structured identities. The chain
//! domain identifies journal entries and deterministic runtime links. Content
//! bytes stay outside this enum because plain BLAKE3 makes equal bytes share
//! one address.

use crate::hash::Hash;

/// Selects one active derived-identifier namespace.
///
/// # Examples
///
/// ```
/// use zeta_substrate::Domain;
///
/// assert_ne!(Domain::Object.context(), Domain::Derivation.context());
/// ```
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Domain {
    /// Immutable substrate objects.
    Object,
    /// Substrate provenance edges.
    Derivation,
    /// Journal entries and deterministic runtime chain links.
    Chain,
}

impl Domain {
    /// Returns the domain's frozen derive-key context string.
    ///
    /// # Examples
    ///
    /// ```
    /// use zeta_substrate::Domain;
    ///
    /// assert_eq!(Domain::Object.context(), "zeta-os 2026-08 cas object");
    /// ```
    pub fn context(self) -> &'static str {
        match self {
            Domain::Object => "zeta-os 2026-08 cas object",
            Domain::Derivation => "zeta-os 2026-08 cas derivation",
            Domain::Chain => "zeta-os 2026-08 cas chain",
        }
    }
}

/// Returns the derived identifier for identity bytes in one domain.
///
/// This is BLAKE3 derive-key mode with the domain's frozen context;
/// the same bytes produce different addresses in different domains,
/// which is the point.
///
/// # Examples
///
/// ```
/// use zeta_substrate::{derive, Domain};
///
/// let object_id = derive(Domain::Object, b"identity");
/// let chain_id = derive(Domain::Chain, b"identity");
/// assert_ne!(object_id, chain_id);
/// ```
pub fn derive(domain: Domain, input: &[u8]) -> Hash {
    let mut hasher = blake3::Hasher::new_derive_key(domain.context());
    hasher.update(input);
    Hash::from_bytes(*hasher.finalize().as_bytes())
}
