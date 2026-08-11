//! Pinned golden digests: the drift tripwire.
//!
//! The expected hex strings are hardcoded on purpose. A dependency
//! bump that changes any output must fail here loudly, because hash
//! stability is the contract this crate exists to keep. stagefs
//! carries its own equivalent pins through its format fixtures — two
//! independent alarms, no shared wiring.

use std::path::Path;

const GOLDEN_BYTES: &[u8] = b"zeta-substrate golden pin: one hash universe\n";
const GOLDEN_BYTES_HEX: &str = "1dfbf2dfcb5a4a03a6ef13a7aaf03cfc00eed50b03601acba5da7b7a4848fab6";
const GOLDEN_FILE_HEX: &str = "49e23e8f078ffc54755d6b1136c4247a6cf2bbb2013af2cc099e1374da9543a4";

#[test]
fn golden_bytes_digest_is_pinned() {
    let hash = zeta_substrate::hash_bytes(GOLDEN_BYTES);
    assert_eq!(hash.to_hex(), GOLDEN_BYTES_HEX);
    assert_eq!(hash.to_string(), format!("b3:{GOLDEN_BYTES_HEX}"));
}

#[test]
fn golden_file_digest_is_pinned_and_matches_hash_bytes() {
    let path = Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/golden.bin");
    let hash = zeta_substrate::hash_file(&path).unwrap();
    assert_eq!(hash.to_hex(), GOLDEN_FILE_HEX);
    let bytes = std::fs::read(&path).unwrap();
    assert_eq!(zeta_substrate::hash_bytes(&bytes), hash);
    assert!(bytes.len() > 16 * 1024, "fixture must exercise the mmap path");
}

#[test]
fn empty_input_digest_is_pinned() {
    assert_eq!(
        zeta_substrate::hash_bytes(b"").to_hex(),
        "af1349b9f5f9a1a6a0404dea36dcc9499bcb25c9adc112b7cc9a93cae41f3262"
    );
}
