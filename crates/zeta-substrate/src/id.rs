//! Identifier epoch recognition for dual-read.
//!
//! The journal keeps every identifier it ever stored, so lookups and
//! comparisons must recognize the sha256 epoch next to modern `b3:`
//! addresses. Recognition lives here once so Phase 2's journal does
//! not re-derive the rules. The shapes mirror the Python
//! `addresses.is_legacy` contract exactly.

use crate::hash::Hash;

/// The recognized shape of one identifier string.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Id {
    /// A modern full-width `b3:` address.
    Modern(Hash),
    /// A legacy bare 24-hex digest (the old truncated handles).
    LegacySha24,
    /// A legacy bare 64-hex digest (the old full sha256 digests).
    LegacySha64,
    /// A legacy `sha256:`-prefixed content address.
    LegacySha256Prefixed,
    /// Any other identifier (`evt_…`, `run_…`, and friends).
    Opaque,
}

impl Id {
    /// Returns whether this identifier belongs to the sha256 epoch.
    ///
    /// # Examples
    ///
    /// ```
    /// use zeta_substrate::parse_id;
    ///
    /// assert!(parse_id(&"a".repeat(24)).is_legacy());
    /// assert!(!parse_id("evt_0123").is_legacy());
    /// ```
    pub fn is_legacy(&self) -> bool {
        match self {
            Id::Modern(_) => false,
            Id::LegacySha24 => true,
            Id::LegacySha64 => true,
            Id::LegacySha256Prefixed => true,
            Id::Opaque => false,
        }
    }
}

/// Recognizes the epoch of one identifier string.
///
/// A `sha256:`-prefixed value is legacy regardless of what follows;
/// a bare string of exactly 24 or 64 lowercase hex characters is a
/// legacy digest; a well-formed `b3:` address is modern; everything
/// else is an opaque identifier that no hash epoch owns.
///
/// # Examples
///
/// ```
/// use zeta_substrate::{parse_id, Id};
///
/// let hash = zeta_substrate::hash_bytes(b"x");
/// assert_eq!(parse_id(&hash.to_string()), Id::Modern(hash));
/// assert_eq!(parse_id("sha256:deadbeef"), Id::LegacySha256Prefixed);
/// assert_eq!(parse_id("qi_evt_1_agent"), Id::Opaque);
/// ```
pub fn parse_id(text: &str) -> Id {
    if text.starts_with("sha256:") {
        return Id::LegacySha256Prefixed;
    }
    if let Ok(hash) = text.parse::<Hash>() {
        return Id::Modern(hash);
    }
    if text.contains(':') {
        return Id::Opaque;
    }
    let mut all_hex = !text.is_empty();
    for character in text.chars() {
        if !is_lower_hex(character) {
            all_hex = false;
            break;
        }
    }
    if all_hex && text.len() == 24 {
        return Id::LegacySha24;
    }
    if all_hex && text.len() == 64 {
        return Id::LegacySha64;
    }
    Id::Opaque
}

fn is_lower_hex(character: char) -> bool {
    character.is_ascii_digit() || ('a'..='f').contains(&character)
}
