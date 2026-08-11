//! Conformance against shared IPC fixtures.

use std::path::{Path, PathBuf};

use serde_json::Value;
use zeta_ipc::{validate_message, Message, RequestId};

fn vectors_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../spec/vectors/ipc")
}

fn json_files(directory: &Path) -> Vec<PathBuf> {
    let mut paths = Vec::new();
    for entry in std::fs::read_dir(directory).unwrap() {
        let path = entry.unwrap().path();
        if path.extension().and_then(|extension| extension.to_str()) == Some("json") {
            paths.push(path);
        }
    }
    paths.sort();
    paths
}

#[test]
fn every_valid_message_vector_parses() {
    let paths = json_files(&vectors_root().join("messages/valid"));
    assert!(
        !paths.is_empty(),
        "the shared valid vector set must not be empty"
    );

    for path in paths {
        let text = std::fs::read_to_string(&path).unwrap();
        let message = Message::parse_str(text.trim_end()).unwrap_or_else(|error| {
            panic!("{path:?} must parse: {error}");
        });
        validate_message(&message).unwrap_or_else(|error| {
            panic!("{path:?} must satisfy protocol parameters: {error}");
        });
    }
}

#[test]
fn every_invalid_message_vector_is_rejected() {
    let paths = json_files(&vectors_root().join("messages/invalid"));
    assert!(
        !paths.is_empty(),
        "the shared invalid vector set must not be empty"
    );

    for path in paths {
        let text = std::fs::read_to_string(&path).unwrap();
        let reason_path = path.with_extension("reason.txt");
        let expected_code: i64 = std::fs::read_to_string(&reason_path)
            .unwrap()
            .lines()
            .next()
            .unwrap()
            .parse()
            .unwrap();
        let error = match Message::parse_str(text.trim_end()) {
            Ok(message) => validate_message(&message).unwrap_err(),
            Err(error) => error,
        };
        assert_eq!(error.code, expected_code, "{path:?}");
    }
}

#[test]
fn every_session_line_has_a_direction_and_a_valid_message() {
    let directory = vectors_root().join("sessions");
    let mut paths = Vec::new();
    for entry in std::fs::read_dir(&directory).unwrap() {
        let path = entry.unwrap().path();
        if path.extension().and_then(|extension| extension.to_str()) == Some("jsonl") {
            paths.push(path);
        }
    }
    paths.sort();
    assert!(
        !paths.is_empty(),
        "the shared session vector set must not be empty"
    );

    for path in paths {
        let text = std::fs::read_to_string(&path).unwrap();
        for (index, line) in text.lines().enumerate() {
            let mut value: Value = serde_json::from_str(line).unwrap();
            let Value::Object(fields) = &mut value else {
                panic!("{path:?}:{} must be an object", index + 1);
            };
            let Some(direction) = fields.remove("_dir") else {
                panic!("{path:?}:{} must carry _dir", index + 1);
            };
            assert!(
                direction == "peer_to_runtime" || direction == "runtime_to_peer",
                "{path:?}:{} has an invalid direction",
                index + 1
            );
            let message = Message::parse_value(&value).unwrap_or_else(|error| {
                panic!("{path:?}:{} must parse: {error}", index + 1);
            });
            validate_message(&message).unwrap_or_else(|error| {
                panic!(
                    "{path:?}:{} must satisfy protocol parameters: {error}",
                    index + 1
                );
            });
        }
    }
}

#[test]
fn request_ids_accept_strings_and_integral_numbers() {
    for (value, expected) in [
        (serde_json::json!("request-1"), RequestId::from("request-1")),
        (serde_json::json!(0), RequestId::from(0_u64)),
        (serde_json::json!(-1), RequestId::from(-1_i64)),
        (serde_json::json!(u64::MAX), RequestId::from(u64::MAX)),
    ] {
        assert_eq!(RequestId::parse(&value).unwrap(), expected);
    }

    for value in [Value::Null, serde_json::json!(1.5), Value::Bool(true)] {
        assert!(RequestId::parse(&value).is_err());
    }
}

#[test]
fn message_classification_uses_discriminant_members() {
    for (value, class) in [
        (
            serde_json::json!({
                "jsonrpc": "2.0", "id": 1, "method": "ping", "params": {},
                "future": true
            }),
            "request",
        ),
        (
            serde_json::json!({"jsonrpc": "2.0", "method": "event", "params": {}}),
            "notification",
        ),
        (
            serde_json::json!({"jsonrpc": "2.0", "id": 1, "result": null}),
            "success",
        ),
        (
            serde_json::json!({
                "jsonrpc": "2.0", "id": null,
                "error": {"code": -32700, "message": "Parse error"}
            }),
            "error",
        ),
    ] {
        let message = Message::parse_value(&value).unwrap();
        let actual = match message {
            Message::Request(_) => "request",
            Message::Notification(_) => "notification",
            Message::Success(_) => "success",
            Message::Error(_) => "error",
        };
        assert_eq!(actual, class);
    }

    for value in [
        serde_json::json!({
            "jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}, "result": {}
        }),
        serde_json::json!({
            "jsonrpc": "2.0", "id": 1, "result": {},
            "error": {"code": -32603, "message": "Internal error"}
        }),
        serde_json::json!({"jsonrpc": "2.0", "id": null, "method": "ping"}),
    ] {
        assert!(Message::parse_value(&value).is_err());
    }
}

#[test]
fn absent_params_are_an_empty_object_and_non_object_params_are_invalid() {
    let message =
        Message::parse_value(&serde_json::json!({"jsonrpc": "2.0", "id": 1, "method": "ping"}))
            .unwrap();
    let Message::Request(request) = message else {
        panic!("the message must be a request");
    };
    assert!(request.params.is_empty());

    let error = Message::parse_value(
        &serde_json::json!({"jsonrpc": "2.0", "id": 1, "method": "ping", "params": []}),
    )
    .unwrap_err();
    assert_eq!(error.code, zeta_ipc::INVALID_REQUEST);
}
