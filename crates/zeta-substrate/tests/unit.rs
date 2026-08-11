//! Behavior tests for hashes, epoch recognition, and the blob store.

use std::path::Path;

use zeta_substrate::{parse_id, BlobStore, Hash, HashParseError, Id, Layout};

#[test]
fn hash_display_and_from_str_round_trip() {
    let hash = zeta_substrate::hash_bytes(b"round trip");
    let text = hash.to_string();
    assert!(text.starts_with("b3:"));
    assert_eq!(text.len(), 3 + 64);
    assert_eq!(text.parse::<Hash>().unwrap(), hash);
}

#[test]
fn hash_from_str_rejects_malformed_input() {
    assert_eq!(
        "af1349".parse::<Hash>(),
        Err(HashParseError::MissingPrefix)
    );
    assert_eq!(
        "b3:abc".parse::<Hash>(),
        Err(HashParseError::BadLength(3))
    );
    let uppercase = format!("b3:{}", "A".repeat(64));
    assert_eq!(
        uppercase.parse::<Hash>(),
        Err(HashParseError::BadDigit('A'))
    );
}

#[test]
fn hash_serde_uses_the_prefixed_string_form() {
    let hash = zeta_substrate::hash_bytes(b"serde");
    let json = serde_json::to_string(&hash).unwrap();
    assert_eq!(json, format!("\"{hash}\""));
    let parsed: Hash = serde_json::from_str(&json).unwrap();
    assert_eq!(parsed, hash);
    let bad: Result<Hash, _> = serde_json::from_str("\"b3:nope\"");
    assert!(bad.is_err());
}

#[test]
fn hash_file_matches_hash_bytes_for_small_files() {
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("small.txt");
    std::fs::write(&path, b"small file, read path").unwrap();
    assert_eq!(
        zeta_substrate::hash_file(&path).unwrap(),
        zeta_substrate::hash_bytes(b"small file, read path")
    );
}

#[test]
fn parse_id_recognizes_every_epoch() {
    let modern = zeta_substrate::hash_bytes(b"modern");
    assert_eq!(parse_id(&modern.to_string()), Id::Modern(modern));
    assert_eq!(parse_id(&"a1".repeat(12)), Id::LegacySha24);
    assert_eq!(parse_id(&"a1".repeat(32)), Id::LegacySha64);
    assert_eq!(parse_id("sha256:whatever"), Id::LegacySha256Prefixed);
    assert_eq!(parse_id("evt_0af3"), Id::Opaque);
    assert_eq!(parse_id("qi_evt_1_agent"), Id::Opaque);
    assert_eq!(parse_id(""), Id::Opaque);
    assert_eq!(parse_id(&"A1".repeat(12)), Id::Opaque);
    assert_eq!(parse_id(&"b3:".repeat(1)), Id::Opaque);
}

#[test]
fn parse_id_legacy_flag_matches_the_python_contract() {
    assert!(parse_id(&"f".repeat(24)).is_legacy());
    assert!(parse_id(&"f".repeat(64)).is_legacy());
    assert!(parse_id("sha256:abc").is_legacy());
    assert!(!parse_id(&zeta_substrate::hash_bytes(b"x").to_string()).is_legacy());
    assert!(!parse_id(&"f".repeat(32)).is_legacy());
}

#[test]
fn blob_store_put_get_and_verify_round_trip() {
    let directory = tempfile::tempdir().unwrap();
    let store = BlobStore::new(directory.path(), Layout::Fanout2);
    let hash = store.put(b"the exact payload bytes").unwrap();
    assert_eq!(store.get(&hash).unwrap(), b"the exact payload bytes");
    assert_eq!(
        store.read_verified(&hash).unwrap(),
        b"the exact payload bytes"
    );
    let again = store.put(b"the exact payload bytes").unwrap();
    assert_eq!(again, hash);
}

#[test]
fn blob_store_layouts_shape_paths_as_documented() {
    let hash = zeta_substrate::hash_bytes(b"layout");
    let hex = hash.to_hex();
    let flat = BlobStore::new(Path::new("/store"), Layout::Flat);
    assert_eq!(flat.path_of(&hash), Path::new("/store/blobs").join(&hex));
    let fanout = BlobStore::new(Path::new("/store"), Layout::Fanout2);
    assert_eq!(
        fanout.path_of(&hash),
        Path::new("/store/blobs").join(&hex[..2]).join(&hex[2..])
    );
}

#[test]
fn blob_store_read_verified_names_a_corrupt_blob() {
    let directory = tempfile::tempdir().unwrap();
    let store = BlobStore::new(directory.path(), Layout::Flat);
    let hash = store.put(b"pristine").unwrap();
    std::fs::write(store.path_of(&hash), b"tampered").unwrap();
    assert_eq!(store.get(&hash).unwrap(), b"tampered");
    let error = store.read_verified(&hash).unwrap_err();
    assert_eq!(error.kind(), std::io::ErrorKind::InvalidData);
    assert!(error.to_string().contains(&hash.to_string()));
}

#[test]
fn blob_store_flat_layout_matches_the_pack_contract() {
    let directory = tempfile::tempdir().unwrap();
    let store = BlobStore::new(directory.path(), Layout::Flat);
    let hash = store.put(b"pack blob").unwrap();
    let expected = directory.path().join("blobs").join(hash.to_hex());
    assert!(expected.is_file());
}
