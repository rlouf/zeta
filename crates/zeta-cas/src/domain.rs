//! Derived-identifier domains and their frozen derive-key contexts.
//!
//! Domain separation exists because derived identifiers name
//! structured identity, and confusing one namespace with another is
//! the real risk. The context strings are frozen forever, verbatim;
//! they are opaque bytes, and a product rename never changes them,
//! because changing one would silently re-address every stored
//! record in its domain. A fifth context, `zeta-os 2026-08 cas blob`,
//! was retired before use (spec §11): content bytes are plain-hashed,
//! never domain-separated, and that string stays reserved so nothing
//! else ever claims it.

use crate::hash::Hash;

/// One derived-identifier domain from spec §11.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Domain {
    /// Wire event envelope ids (spec §6.1).
    Event,
    /// Links in the idempotent id chain (publish and wait handles).
    Chain,
    /// Records in the prompt-trace substrate.
    Prompt,
    /// Skill body identifiers.
    Skill,
}

impl Domain {
    /// Returns the frozen derive-key context string, verbatim.
    ///
    /// # Examples
    ///
    /// ```
    /// use zeta_cas::Domain;
    ///
    /// assert_eq!(Domain::Event.context(), "zeta-os 2026-08 cas event");
    /// ```
    pub fn context(self) -> &'static str {
        match self {
            Domain::Event => "zeta-os 2026-08 cas event",
            Domain::Chain => "zeta-os 2026-08 cas chain",
            Domain::Prompt => "zeta-os 2026-08 cas prompt",
            Domain::Skill => "zeta-os 2026-08 cas skill",
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
/// use zeta_cas::{derive, Domain};
///
/// let event_id = derive(Domain::Event, b"identity");
/// let chain_id = derive(Domain::Chain, b"identity");
/// assert_ne!(event_id, chain_id);
/// ```
pub fn derive(domain: Domain, input: &[u8]) -> Hash {
    let mut hasher = blake3::Hasher::new_derive_key(domain.context());
    hasher.update(input);
    Hash::from_bytes(*hasher.finalize().as_bytes())
}
