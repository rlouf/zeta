//! Conformance against the shared golden vectors, read in place.
//!
//! The same files drive the Python implementation. Reading them from
//! the repo — never copying — is what keeps two implementations one
//! protocol: a vector change breaks whichever side stopped
//! conforming.

use std::path::PathBuf;
use std::time::{Duration, Instant};

use serde_json::{Map, Value};
use zeta_wire::session::{
    Action, PluginConfig, PluginSession, RuntimeConfig, RuntimeSession,
};
use zeta_wire::{validate_envelope, Envelope, EventTypeDecl};

fn vectors_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../spec/vectors")
}

fn read_json(path: &PathBuf) -> Value {
    let text = std::fs::read_to_string(path).unwrap();
    serde_json::from_str(&text).unwrap()
}

#[test]
fn every_valid_vector_parses_and_reserializes_canonically() {
    let directory = vectors_root().join("envelopes/valid");
    let mut checked = 0;
    for entry in std::fs::read_dir(&directory).unwrap() {
        let path = entry.unwrap().path();
        if path.extension().and_then(|extension| extension.to_str()) != Some("json") {
            continue;
        }
        let text = std::fs::read_to_string(&path).unwrap();
        let envelope = Envelope::parse_str(text.trim_end()).unwrap_or_else(|error| {
            panic!("{path:?} must parse: {error}");
        });
        assert_eq!(
            envelope.to_canonical_json(),
            text.trim_end(),
            "{path:?} must round-trip byte-for-byte"
        );
        checked += 1;
    }
    assert!(checked >= 16, "expected the full valid set, saw {checked}");
}

#[test]
fn every_invalid_vector_fails_for_its_documented_rule() {
    let directory = vectors_root().join("envelopes/invalid");
    let mut checked = 0;
    for entry in std::fs::read_dir(&directory).unwrap() {
        let path = entry.unwrap().path();
        if path.extension().and_then(|extension| extension.to_str()) != Some("json") {
            continue;
        }
        let reason_path = path.with_file_name(format!(
            "{}.reason.txt",
            path.file_stem().unwrap().to_str().unwrap()
        ));
        let reason = std::fs::read_to_string(&reason_path).unwrap();
        let documented_rule = reason.lines().next().unwrap();
        let value = read_json(&path);
        let error = validate_envelope(&value).unwrap_err();
        assert_eq!(error.rule, documented_rule, "{path:?}");
        checked += 1;
    }
    assert!(checked >= 69, "expected the full invalid set, saw {checked}");
}

struct SessionLine {
    direction: String,
    envelope: Envelope,
    value: Value,
}

fn session_lines(name: &str) -> Vec<SessionLine> {
    let path = vectors_root().join("handshake").join(name);
    let text = std::fs::read_to_string(&path).unwrap();
    let mut lines = Vec::new();
    for line in text.lines() {
        let mut value: Value = serde_json::from_str(line).unwrap();
        let direction = value
            .as_object_mut()
            .unwrap()
            .remove("_dir")
            .unwrap()
            .as_str()
            .unwrap()
            .to_string();
        let envelope = Envelope::parse_value(&value).unwrap();
        lines.push(SessionLine {
            direction,
            envelope,
            value,
        });
    }
    lines
}

fn field<'a>(value: &'a Value, name: &str) -> &'a Value {
    value.get(name).unwrap()
}

#[test]
fn session_01_replays_through_the_runtime_side() {
    let lines = session_lines("session-01.jsonl");
    let now = Instant::now();
    let mut session = RuntimeSession::new(RuntimeConfig::default(), now);
    let mut expected_sends = Vec::new();
    for SessionLine {
        direction, value, ..
    } in &lines
    {
        if direction == "p2c" {
            expected_sends.push(value.clone());
        }
    }
    for SessionLine {
        direction,
        envelope,
        value,
    } in &lines
    {
        let _ = value;
        if direction == "p2c" {
            continue;
        }
        let actions = session.on_envelope(envelope, now, "2026-08-10T12:00:00Z");
        for action in actions {
            match action {
                Action::Send(sent) => check_send(&sent, &mut expected_sends),
                Action::DeliverEvent(event) => {
                    let acks = session.acknowledge(&event.common.id, "2026-08-10T12:00:01Z");
                    let [Action::Send(ack)] = acks.as_slice() else {
                        panic!("a durable event earns exactly one ack");
                    };
                    check_send(ack, &mut expected_sends);
                }
                Action::HandleCall(_)
                | Action::CallResolved(_)
                | Action::PeerError(_)
                | Action::ProtocolViolation { .. }
                | Action::Kill { .. }
                | Action::Exit { .. } => panic!("unexpected action in a clean replay"),
            }
        }
    }
    let sends = session.shutdown("runtime stopping", "2026-08-10T12:00:12Z");
    let [Action::Send(shutdown)] = sends.as_slice() else {
        panic!("shutdown emits one envelope");
    };
    check_send(shutdown, &mut expected_sends);
    assert!(expected_sends.is_empty(), "every p2c line must be produced");
}

fn check_send(sent: &Envelope, expected_sends: &mut Vec<Value>) {
    assert!(!expected_sends.is_empty(), "unexpected extra send {sent:?}");
    let expected = expected_sends.remove(0);
    let sent = sent.to_value();
    assert_eq!(field(&sent, "kind"), field(&expected, "kind"));
    match field(&expected, "kind").as_str().unwrap() {
        "hello_ack" => {
            assert_eq!(
                field(&sent, "protocol_version"),
                field(&expected, "protocol_version")
            );
            assert_eq!(
                sent.get("config").is_some(),
                expected.get("config").is_some()
            );
        }
        "ack" => {
            assert_eq!(field(&sent, "event_id"), field(&expected, "event_id"));
        }
        "shutdown" => {
            assert_eq!(field(&sent, "reason"), field(&expected, "reason"));
        }
        "call" => {
            assert_eq!(field(&sent, "name"), field(&expected, "name"));
            assert_eq!(field(&sent, "payload"), field(&expected, "payload"));
            assert_eq!(field(&sent, "effect_key"), field(&expected, "effect_key"));
        }
        other => panic!("unexpected p2c kind {other:?} in the vector"),
    }
}

#[test]
fn session_01_replays_through_the_plugin_side() {
    let lines = session_lines("session-01.jsonl");
    let hello = &lines[0].value;
    let mut event_types = Vec::new();
    for entry in field(hello, "event_types").as_array().unwrap() {
        event_types.push(EventTypeDecl {
            event_type: field(entry, "type").as_str().unwrap().to_string(),
            schema: field(entry, "schema").as_str().unwrap().to_string(),
            extra: Map::new(),
        });
    }
    let mut config = PluginConfig::source(
        field(hello, "name").as_str().unwrap(),
        field(hello, "plugin_version").as_str().unwrap(),
        event_types,
    );
    config.capabilities = Some(
        field(hello, "capabilities").as_object().unwrap().clone(),
    );
    let mut session = PluginSession::new(config);
    let now = Instant::now();

    let started = session.start(now, "2026-08-10T12:00:00Z");
    let [Action::Send(Envelope::Hello(sent))] = started.as_slice() else {
        panic!("start emits the hello");
    };
    let sent = serde_json::to_value(sent).unwrap();
    for name in [
        "name",
        "plugin_version",
        "role",
        "protocol_versions",
        "event_types",
        "capabilities",
        "heartbeat_secs",
        "ack_window",
    ] {
        assert_eq!(field(&sent, name), field(hello, name), "hello field {name}");
    }

    for SessionLine {
        direction,
        envelope,
        value,
    } in &lines[1..]
    {
        if direction == "p2c" {
            let actions = session.on_envelope(envelope, now);
            match envelope {
                Envelope::Shutdown(_) => {
                    let [Action::Exit { .. }] = actions.as_slice() else {
                        panic!("shutdown exits the plugin");
                    };
                }
                Envelope::HelloAck(_) | Envelope::Ack(_) => assert!(actions.is_empty()),
                Envelope::Hello(_)
                | Envelope::Event(_)
                | Envelope::Heartbeat(_)
                | Envelope::Error(_)
                | Envelope::Call(_)
                | Envelope::CallResult(_) => {
                    panic!("unexpected p2c kind in session-01")
                }
            }
            continue;
        }
        match envelope {
            Envelope::Event(event) => {
                let sends = session
                    .send_event(
                        &event.event_type,
                        event.payload.clone().unwrap(),
                        event.caused_by.clone(),
                        event.session_id.clone(),
                        "2026-08-10T12:00:01Z",
                    )
                    .unwrap();
                let [Action::Send(Envelope::Event(sent))] = sends.as_slice() else {
                    panic!("send_event emits the event");
                };
                assert_eq!(
                    sent.common.id, event.common.id,
                    "the deterministic event id must match the vector"
                );
                let sent = serde_json::to_value(sent).unwrap();
                for name in ["type", "schema", "caused_by", "session_id", "payload"] {
                    assert_eq!(field(&sent, name), field(value, name), "event {name}");
                }
            }
            Envelope::Heartbeat(_) => {
                let ticked =
                    session.on_tick(now + Duration::from_secs(11), "2026-08-10T12:00:10Z");
                let [Action::Send(Envelope::Heartbeat(_))] = ticked.as_slice() else {
                    panic!("a due tick emits a heartbeat");
                };
            }
            Envelope::Hello(_) => {}
            Envelope::HelloAck(_)
            | Envelope::Ack(_)
            | Envelope::Error(_)
            | Envelope::Shutdown(_)
            | Envelope::Call(_)
            | Envelope::CallResult(_) => panic!("unexpected c2p kind in session-01"),
        }
    }
}

#[test]
fn session_02_replays_through_both_sides() {
    let lines = session_lines("session-02.jsonl");
    let now = Instant::now();
    let wall = "2026-08-10T12:00:00Z";

    let mut runtime = RuntimeSession::new(
        RuntimeConfig {
            config: Some(Map::new()),
            ..RuntimeConfig::default()
        },
        now,
    );
    let hello = &lines[0];
    assert_eq!(hello.direction, "c2p");
    let actions = runtime.on_envelope(&hello.envelope, now, wall);
    let [Action::Send(Envelope::HelloAck(ack))] = actions.as_slice() else {
        panic!("hello earns hello_ack");
    };
    assert_eq!(ack.config, Some(Map::new()));

    let vector_call = &lines[2];
    let Envelope::Call(expected_call) = &vector_call.envelope else {
        panic!("session-02 line 3 is the call");
    };
    let sends = runtime
        .send_call(
            &expected_call.name,
            expected_call.payload.clone(),
            &expected_call.effect_key,
            wall,
        )
        .unwrap();
    let [Action::Send(Envelope::Call(sent_call))] = sends.as_slice() else {
        panic!("send_call emits the call");
    };
    assert_eq!(sent_call.name, expected_call.name);
    assert_eq!(sent_call.payload, expected_call.payload);
    assert_eq!(sent_call.effect_key, expected_call.effect_key);

    let vector_result = &lines[3];
    let Envelope::CallResult(expected_result) = &vector_result.envelope else {
        panic!("session-02 line 4 is the call_result");
    };
    let mut rewritten = expected_result.clone();
    rewritten.call_id = sent_call.common.id.clone();
    let actions = runtime.on_envelope(&Envelope::CallResult(rewritten), now, wall);
    let [Action::CallResolved(info)] = actions.as_slice() else {
        panic!("the call_result resolves the call");
    };
    assert_eq!(info.outcome, Ok(expected_result.result.clone().unwrap()));

    let Envelope::Hello(vector_hello) = &hello.envelope else {
        panic!("session-02 opens with hello");
    };
    let mut operations = Vec::new();
    for declared in vector_hello.operations.as_ref().unwrap() {
        operations.push(declared.name.clone());
    }
    let mut config = PluginConfig::source(
        &vector_hello.name,
        &vector_hello.plugin_version,
        vector_hello.event_types.clone().unwrap(),
    );
    config.operations = operations;
    let mut plugin = PluginSession::new(config);
    plugin.start(now, wall);
    assert!(plugin.runtime_config().is_none());
    assert!(plugin.on_envelope(&lines[1].envelope, now).is_empty());
    assert_eq!(plugin.runtime_config(), Some(&Map::new()));
    let handled = plugin.on_envelope(&vector_call.envelope, now);
    let [Action::HandleCall(call)] = handled.as_slice() else {
        panic!("the call reaches the handler");
    };
    let completed = plugin
        .complete_call(
            &call.common.id,
            Ok(expected_result.result.clone().unwrap()),
            wall,
        )
        .unwrap();
    let [Action::Send(Envelope::CallResult(answer))] = completed.as_slice() else {
        panic!("complete_call emits the call_result");
    };
    assert!(answer.ok);
    assert_eq!(answer.result, expected_result.result);
    assert_eq!(answer.call_id, call.common.id);

    let shutdown = &lines[5];
    let exited = plugin.on_envelope(&shutdown.envelope, now);
    let [Action::Exit { .. }] = exited.as_slice() else {
        panic!("shutdown exits the plugin");
    };
}
