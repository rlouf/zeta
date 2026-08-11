//! Behavior tests for hashes, substrate values, domains, and blob storage.

use std::path::Path;

use serde_json::{json, Map, Number, Value};
use zeta_substrate::{BlobStore, Derivation, Domain, Hash, HashParseError, Object, Ref, RefUpdate};

fn fields(value: serde_json::Value) -> Map<String, serde_json::Value> {
    value.as_object().unwrap().clone()
}

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
    assert_eq!("af1349".parse::<Hash>(), Err(HashParseError::MissingPrefix));
    assert_eq!("b3:abc".parse::<Hash>(), Err(HashParseError::BadLength(3)));
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
fn active_domains_have_distinct_frozen_contexts() {
    assert_eq!(Domain::Object.context(), "zeta-os 2026-08 cas object");
    assert_eq!(
        Domain::Derivation.context(),
        "zeta-os 2026-08 cas derivation"
    );
    assert_eq!(Domain::Chain.context(), "zeta-os 2026-08 cas chain");

    let input = b"same identity";
    let domains = [Domain::Object, Domain::Derivation, Domain::Chain];
    for (index, domain) in domains.iter().enumerate() {
        for other in &domains[index + 1..] {
            assert_ne!(
                zeta_substrate::derive(*domain, input),
                zeta_substrate::derive(*other, input)
            );
        }
    }
}

#[test]
fn object_content_address_uses_canonical_fields_and_object_domain() {
    let object = Object {
        kind: "example.message".to_owned(),
        schema: "zeta.example.v1".to_owned(),
        data: fields(json!({"z": 2, "a": "héllo"})),
        links: vec![zeta_substrate::hash_bytes(b"child").to_string()],
    };
    let canonical = concat!(
        "{\"data\":{\"a\":\"héllo\",\"z\":2},",
        "\"kind\":\"example.message\",",
        "\"links\":[\"b3:9e3f17c9899155024d1487a756b761dc424fa5e6a32bf164f289f4f00a442205\"],",
        "\"schema\":\"zeta.example.v1\"}"
    );
    assert_eq!(object.canonical_bytes().unwrap(), canonical.as_bytes());
    assert_eq!(
        object.content_address().unwrap(),
        zeta_substrate::derive(Domain::Object, canonical.as_bytes())
    );
}

#[test]
fn derivation_content_address_uses_canonical_fields_and_derivation_domain() {
    let output_id = zeta_substrate::hash_bytes(b"output").to_string();
    let input_id = zeta_substrate::hash_bytes(b"input").to_string();
    let derivation = Derivation {
        producer: "example:combine@1".to_owned(),
        output_id,
        input_ids: vec![input_id],
        params: fields(json!({"temperature": 0.1, "optional": null})),
    };
    let canonical = derivation.canonical_bytes().unwrap();
    assert_eq!(
        derivation.content_address().unwrap(),
        zeta_substrate::derive(Domain::Derivation, &canonical)
    );
    assert_eq!(canonical.first(), Some(&b'{'));
    assert_eq!(canonical.last(), Some(&b'}'));
    assert!(!canonical.ends_with(b"\n"));
}

#[test]
fn canonical_json_rejects_integer_above_u64() {
    let value = serde_json::from_str("18446744073709551616").unwrap();
    assert_eq!(
        zeta_substrate::canonical_json(&value),
        Err(zeta_substrate::CanonicalJsonError::IntegerOutOfRange(
            "18446744073709551616".to_owned()
        ))
    );
}

#[test]
fn canonical_json_rejects_integer_below_i64() {
    let value = serde_json::from_str("-9223372036854775809").unwrap();
    assert_eq!(
        zeta_substrate::canonical_json(&value),
        Err(zeta_substrate::CanonicalJsonError::IntegerOutOfRange(
            "-9223372036854775809".to_owned()
        ))
    );
}

#[test]
fn object_content_address_rejects_float_outside_binary64() {
    let data = serde_json::from_str("{\"value\":1e400}").unwrap();
    let object = Object {
        kind: "example.number".to_owned(),
        schema: "zeta.example.v1".to_owned(),
        data,
        links: Vec::new(),
    };
    assert_eq!(
        object.content_address(),
        Err(zeta_substrate::CanonicalJsonError::FloatOutOfRange(
            "1e400".to_owned()
        ))
    );
}

#[test]
fn programmatic_floats_match_python_spelling() {
    for (number, expected) in [
        (1.0, "1.0"),
        (0.1, "0.1"),
        (1e30, "1e+30"),
        (-0.0, "-0.0"),
        (1e-5, "1e-05"),
        (1e-6, "1e-06"),
        (333333333.3333333, "333333333.3333333"),
    ] {
        let value = Value::Number(Number::from_f64(number).unwrap());
        assert_eq!(
            zeta_substrate::canonical_json(&value).unwrap(),
            expected.as_bytes()
        );
    }
}

#[test]
fn refs_preserve_named_pointer_and_conditional_update_fields() {
    let old_object_id = zeta_substrate::hash_bytes(b"old").to_string();
    let new_object_id = zeta_substrate::hash_bytes(b"new").to_string();
    let reference = Ref {
        name: "session/head".to_owned(),
        object_id: old_object_id.clone(),
    };
    let update = RefUpdate {
        name: reference.name.clone(),
        old_object_id: Some(reference.object_id),
        new_object_id,
        updated: true,
    };
    assert_eq!(update.name, "session/head");
    assert!(update.old_object_id.is_some());
    assert!(update.updated);
}

#[test]
fn blob_store_put_get_and_verify_round_trip() {
    let directory = tempfile::tempdir().unwrap();
    let store = BlobStore::new(directory.path());
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
fn blob_store_uses_two_character_fanout() {
    let hash = zeta_substrate::hash_bytes(b"layout");
    let hex = hash.to_hex();
    let store = BlobStore::new(Path::new("/store"));
    assert_eq!(
        store.path_of(&hash),
        Path::new("/store/blobs").join(&hex[..2]).join(&hex[2..])
    );
}

#[test]
fn blob_store_read_verified_names_a_corrupt_blob() {
    let directory = tempfile::tempdir().unwrap();
    let store = BlobStore::new(directory.path());
    let hash = store.put(b"pristine").unwrap();
    std::fs::write(store.path_of(&hash), b"tampered").unwrap();
    assert_eq!(store.get(&hash).unwrap(), b"tampered");
    let error = store.read_verified(&hash).unwrap_err();
    assert_eq!(error.kind(), std::io::ErrorKind::InvalidData);
    assert!(error.to_string().contains(&hash.to_string()));
}
