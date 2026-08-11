//! The 32-byte BLAKE3 hash and its `b3:` string form.
//!
//! The string form is `b3:` plus exactly 64 lowercase hex characters,
//! never truncated. Parsing is strict so that a hash that round-trips
//! is byte-identical to its source; permissive parsing would let two
//! spellings of one hash exist, which breaks address equality.

use std::fmt;
use std::io;
use std::path::Path;
use std::str::FromStr;

use serde::de::Error as _;
use serde::{Deserialize, Deserializer, Serialize, Serializer};

/// The `b3:` prefix every address string carries.
pub const PREFIX: &str = "b3:";

/// A 32-byte BLAKE3 output with the `b3:<64 lowercase hex>` string form.
///
/// # Examples
///
/// ```
/// use zeta_substrate::Hash;
///
/// let hash = zeta_substrate::hash_bytes(b"hello wire");
/// let text = hash.to_string();
/// assert!(text.starts_with("b3:"));
/// assert_eq!(text.parse::<Hash>().unwrap(), hash);
/// ```
#[derive(Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct Hash([u8; 32]);

impl Hash {
    /// Creates a hash from its raw 32 bytes.
    pub fn from_bytes(bytes: [u8; 32]) -> Self {
        Hash(bytes)
    }

    /// Returns the raw 32 bytes.
    pub fn as_bytes(&self) -> &[u8; 32] {
        let Hash(bytes) = self;
        bytes
    }

    /// Returns the bare 64-character lowercase hex digest.
    ///
    /// This is the form written on disk by blob stores and by the
    /// stagefs pack layout; the wire form adds the `b3:` prefix.
    ///
    /// # Examples
    ///
    /// ```
    /// let hash = zeta_substrate::hash_bytes(b"");
    /// assert_eq!(hash.to_hex().len(), 64);
    /// ```
    pub fn to_hex(&self) -> String {
        let Hash(bytes) = self;
        let mut hex = String::with_capacity(64);
        for byte in bytes {
            hex.push(HEX_DIGITS[usize::from(byte >> 4)]);
            hex.push(HEX_DIGITS[usize::from(byte & 0x0f)]);
        }
        hex
    }
}

const HEX_DIGITS: [char; 16] = [
    '0', '1', '2', '3', '4', '5', '6', '7', '8', '9', 'a', 'b', 'c', 'd', 'e', 'f',
];

impl fmt::Display for Hash {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "{PREFIX}{}", self.to_hex())
    }
}

impl fmt::Debug for Hash {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "Hash({PREFIX}{})", self.to_hex())
    }
}

/// The reason a string is not a well-formed `b3:` address.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum HashParseError {
    /// The string does not start with `b3:`.
    MissingPrefix,
    /// The digest part is not exactly 64 characters.
    BadLength(usize),
    /// The digest part contains a character outside `[0-9a-f]`.
    BadDigit(char),
}

impl fmt::Display for HashParseError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            HashParseError::MissingPrefix => {
                write!(formatter, "a b3 address must start with {PREFIX:?}")
            }
            HashParseError::BadLength(length) => write!(
                formatter,
                "a b3 digest must be 64 hex characters, got {length}"
            ),
            HashParseError::BadDigit(character) => write!(
                formatter,
                "a b3 digest allows only lowercase hex, got {character:?}"
            ),
        }
    }
}

impl std::error::Error for HashParseError {}

impl FromStr for Hash {
    type Err = HashParseError;

    fn from_str(text: &str) -> Result<Self, Self::Err> {
        let Some(digest) = text.strip_prefix(PREFIX) else {
            return Err(HashParseError::MissingPrefix);
        };
        if digest.len() != 64 {
            return Err(HashParseError::BadLength(digest.len()));
        }
        let mut bytes = [0u8; 32];
        let mut characters = digest.chars();
        for byte in &mut bytes {
            let high = hex_value(characters.next().unwrap_or('\0'))?;
            let low = hex_value(characters.next().unwrap_or('\0'))?;
            *byte = (high << 4) | low;
        }
        Ok(Hash(bytes))
    }
}

fn hex_value(character: char) -> Result<u8, HashParseError> {
    match character {
        '0'..='9' => Ok(character as u8 - b'0'),
        'a'..='f' => Ok(character as u8 - b'a' + 10),
        other => Err(HashParseError::BadDigit(other)),
    }
}

impl Serialize for Hash {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        serializer.serialize_str(&self.to_string())
    }
}

impl<'de> Deserialize<'de> for Hash {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        let text = String::deserialize(deserializer)?;
        let hash = text.parse::<Hash>();
        let Ok(hash) = hash else {
            return Err(D::Error::custom(format!(
                "invalid b3 address {text:?}"
            )));
        };
        Ok(hash)
    }
}

/// Returns the plain, undomained BLAKE3 content address of bytes.
///
/// Content hashing carries no domain context on purpose: the same
/// bytes hash to the same address in every tool, which is what makes
/// pack interop a file copy instead of a conversion.
///
/// # Examples
///
/// ```
/// let hash = zeta_substrate::hash_bytes(b"hello wire");
/// assert_eq!(hash, zeta_substrate::hash_bytes(b"hello wire"));
/// ```
pub fn hash_bytes(bytes: &[u8]) -> Hash {
    Hash(*blake3::hash(bytes).as_bytes())
}

/// Returns the plain BLAKE3 content address of a file's bytes.
///
/// Large files hash through a memory map so the whole file never
/// sits in heap memory; small files read normally because mapping
/// them costs more than it saves. The output equals [`hash_bytes`]
/// of the same bytes by construction.
///
/// # Errors
///
/// Returns [`io::Error`] if the file cannot be opened or read.
///
/// [`io::Error`]: std::io::Error
///
/// # Examples
///
/// ```no_run
/// use std::path::Path;
///
/// let hash = zeta_substrate::hash_file(Path::new("data.bin")).unwrap();
/// println!("{hash}");
/// ```
pub fn hash_file(path: &Path) -> io::Result<Hash> {
    let mut hasher = blake3::Hasher::new();
    hasher.update_mmap(path)?;
    Ok(Hash(*hasher.finalize().as_bytes()))
}
