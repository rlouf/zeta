//! Conformance against the shared golden vectors, read in place.
//!
//! The vectors under `spec/vectors/` are the single set of golden
//! files both language implementations must satisfy. This test reads
//! them from the repo — no copies — so the two implementations
//! cannot drift apart without one of them failing.

use std::path::PathBuf;

use serde_json::Value;
use zeta_cas::Domain;

fn vectors_document() -> Value {
    let path = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../spec/vectors/addresses/vectors.json");
    let text = std::fs::read_to_string(&path).unwrap();
    serde_json::from_str(&text).unwrap()
}

fn input_bytes(vector: &Value) -> Vec<u8> {
    if let Some(text) = vector.get("input_utf8") {
        return text.as_str().unwrap().as_bytes().to_vec();
    }
    use base64::Engine as _;
    let encoded = vector["input_base64"].as_str().unwrap();
    base64::engine::general_purpose::STANDARD
        .decode(encoded)
        .unwrap()
}

fn domain_for(name: &str) -> Option<Domain> {
    match name {
        "event" => Some(Domain::Event),
        "chain" => Some(Domain::Chain),
        "prompt" => Some(Domain::Prompt),
        "skill" => Some(Domain::Skill),
        "content" => None,
        other => panic!("unknown vector domain {other:?}"),
    }
}

#[test]
fn frozen_contexts_match_the_vector_document() {
    let document = vectors_document();
    let contexts = document["contexts"].as_object().unwrap();
    assert_eq!(contexts.len(), 4);
    for (name, context) in contexts {
        let Some(domain) = domain_for(name) else {
            panic!("context table must hold derived domains only");
        };
        assert_eq!(domain.context(), context.as_str().unwrap());
    }
}

#[test]
fn every_address_vector_matches_byte_for_byte() {
    let document = vectors_document();
    let vectors = document["vectors"].as_array().unwrap();
    assert!(!vectors.is_empty());
    let mut content_vectors = 0;
    for vector in vectors {
        let bytes = input_bytes(vector);
        let expected = vector["address"].as_str().unwrap();
        let minted = match domain_for(vector["domain"].as_str().unwrap()) {
            Some(domain) => zeta_cas::derive(domain, &bytes),
            None => {
                content_vectors += 1;
                zeta_cas::hash_bytes(&bytes)
            }
        };
        assert_eq!(minted.to_string(), expected, "{vector}");
    }
    assert!(content_vectors > 0, "content vectors must be present");
}
