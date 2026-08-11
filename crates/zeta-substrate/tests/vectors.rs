//! Conformance against the shared canonical encoding vectors.

use std::path::PathBuf;

use serde_json::Value;

fn encoding_document() -> Value {
    let path = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../spec/vectors/substrate/encoding.json");
    let text = std::fs::read_to_string(&path).unwrap();
    serde_json::from_str(&text).unwrap()
}

#[test]
fn every_python_encoding_vector_matches_byte_for_byte() {
    let document = encoding_document();
    let vectors = document["vectors"].as_array().unwrap();
    assert!(!vectors.is_empty());
    for vector in vectors {
        let value = &vector["value"];
        let expected = vector["canonical_utf8"].as_str().unwrap().as_bytes();
        assert_eq!(
            zeta_substrate::canonical_json(value).unwrap(),
            expected,
            "{}",
            vector["name"]
        );
    }
}
