//! Conformance against the shared canonical encoding vectors.

use std::path::PathBuf;

use serde_json::{Map, Value};
use zeta::substrate::{Derivation, Object};

fn encoding_document() -> Value {
    let path = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../spec/vectors/substrate/encoding.json");
    let text = std::fs::read_to_string(&path).unwrap();
    serde_json::from_str(&text).unwrap()
}

fn address_document() -> Value {
    let path = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../spec/vectors/substrate/b3-addresses.json");
    let text = std::fs::read_to_string(&path).unwrap();
    serde_json::from_str(&text).unwrap()
}

fn object_fields(value: &Value) -> Map<String, Value> {
    value.as_object().unwrap().clone()
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
            zeta::substrate::canonical_json(value).unwrap(),
            expected,
            "{}",
            vector["name"]
        );
    }
}

#[test]
fn every_python_address_vector_matches_byte_for_byte() {
    let document = address_document();
    for vector in document["objects"].as_array().unwrap() {
        let fields = &vector["object"];
        let mut links = Vec::new();
        for value in fields["links"].as_array().unwrap() {
            links.push(value.as_str().unwrap().to_owned());
        }
        let object = Object {
            kind: fields["kind"].as_str().unwrap().to_owned(),
            schema: fields["schema"].as_str().unwrap().to_owned(),
            data: object_fields(&fields["data"]),
            links,
        };
        assert_eq!(
            object.canonical_bytes().unwrap(),
            vector["canonical_utf8"].as_str().unwrap().as_bytes(),
            "{}",
            vector["name"]
        );
        assert_eq!(
            object.content_address().unwrap().to_string(),
            vector["address"],
            "{}",
            vector["name"]
        );
    }
    for vector in document["derivations"].as_array().unwrap() {
        let fields = &vector["derivation"];
        let mut input_ids = Vec::new();
        for value in fields["input_ids"].as_array().unwrap() {
            input_ids.push(value.as_str().unwrap().to_owned());
        }
        let derivation = Derivation {
            producer: fields["producer"].as_str().unwrap().to_owned(),
            output_id: fields["output_id"].as_str().unwrap().to_owned(),
            input_ids,
            params: object_fields(&fields["params"]),
        };
        assert_eq!(
            derivation.canonical_bytes().unwrap(),
            vector["canonical_utf8"].as_str().unwrap().as_bytes(),
            "{}",
            vector["name"]
        );
        assert_eq!(
            derivation.content_address().unwrap().to_string(),
            vector["address"],
            "{}",
            vector["name"]
        );
    }
}
