//! Property tests: serialization fixpoints and machine invariants.

use std::time::Instant;

use proptest::prelude::*;
use serde_json::{Map, Number, Value};
use zeta_wire::session::{Action, PluginConfig, PluginSession, RuntimeConfig, RuntimeSession};
use zeta_wire::{Envelope, EventTypeDecl};

fn identifier() -> impl Strategy<Value = String> {
    "[a-z][a-z0-9-]{0,19}".prop_map(|text| text)
}

fn payload_value() -> impl Strategy<Value = Value> {
    prop_oneof![
        "[a-zA-Z0-9 é✓-]{0,24}".prop_map(Value::String),
        any::<u32>().prop_map(|number| Value::Number(Number::from(number))),
        any::<bool>().prop_map(Value::Bool),
    ]
}

fn payload() -> impl Strategy<Value = Map<String, Value>> {
    proptest::collection::btree_map("[a-z_]{1,12}", payload_value(), 0..6).prop_map(
        |entries| {
            let mut map = Map::new();
            for (key, value) in entries {
                map.insert(key, value);
            }
            map
        },
    )
}

fn envelope_json() -> impl Strategy<Value = Value> {
    let ts = Just(Value::String("2026-08-10T12:00:00Z".to_string()));
    prop_oneof![
        (identifier(), ts.clone()).prop_map(|(id, ts)| {
            serde_json::json!({"v": 0, "kind": "heartbeat", "id": id, "ts": ts})
        }),
        (identifier(), ts.clone(), payload(), identifier()).prop_map(
            |(id, ts, payload, event_type)| {
                serde_json::json!({
                    "v": 0,
                    "kind": "event",
                    "id": id,
                    "ts": ts,
                    "type": event_type,
                    "schema": format!("{event_type}@1"),
                    "caused_by": Value::Null,
                    "session_id": Value::Null,
                    "payload": Value::Object(payload),
                })
            }
        ),
        (identifier(), ts.clone(), identifier()).prop_map(|(id, ts, event_id)| {
            serde_json::json!({
                "v": 0,
                "kind": "ack",
                "id": id,
                "ts": ts,
                "event_id": event_id,
            })
        }),
        (identifier(), ts.clone(), payload(), identifier()).prop_map(
            |(id, ts, payload, name)| {
                serde_json::json!({
                    "v": 0,
                    "kind": "call",
                    "id": id,
                    "ts": ts,
                    "name": name,
                    "payload": Value::Object(payload),
                    "effect_key": format!("effect-{id}"),
                })
            }
        ),
        (identifier(), ts, "[a-zA-Z0-9 ]{1,40}").prop_map(|(id, ts, message)| {
            serde_json::json!({
                "v": 0,
                "kind": "error",
                "id": id,
                "ts": ts,
                "code": "internal",
                "message": message,
                "retryable": false,
            })
        }),
    ]
}

proptest! {
    #[test]
    fn serialize_parse_serialize_is_a_fixpoint(value in envelope_json()) {
        let envelope = Envelope::parse_value(&value).unwrap();
        let first = envelope.to_canonical_json();
        let reparsed = Envelope::parse_str(&first).unwrap();
        prop_assert_eq!(&reparsed, &envelope);
        prop_assert_eq!(reparsed.to_canonical_json(), first);
    }

    #[test]
    fn the_runtime_session_never_accepts_traffic_before_hello(value in envelope_json()) {
        let now = Instant::now();
        let mut session = RuntimeSession::new(RuntimeConfig::default(), now);
        let envelope = Envelope::parse_value(&value).unwrap();
        let actions = session.on_envelope(&envelope, now, "2026-08-10T12:00:00Z");
        let mut killed = false;
        for action in &actions {
            match action {
                Action::Kill { .. } => killed = true,
                Action::DeliverEvent(_) => prop_assert!(false, "delivered pre-handshake"),
                Action::Send(_)
                | Action::HandleCall(_)
                | Action::CallResolved(_)
                | Action::PeerError(_)
                | Action::ProtocolViolation { .. }
                | Action::Exit { .. } => {}
            }
        }
        prop_assert!(killed, "pre-handshake traffic must kill the child");
        prop_assert!(!session.is_established());
    }

    #[test]
    fn the_plugin_session_never_exceeds_its_ack_window(window in 1u64..8) {
        let now = Instant::now();
        let mut config = PluginConfig::source(
            "prop",
            "0",
            vec![EventTypeDecl {
                event_type: "prop.event".to_string(),
                schema: "prop.event@1".to_string(),
                extra: Map::new(),
            }],
        );
        config.ack_window = window;
        let mut session = PluginSession::new(config);
        session.start(now, "2026-08-10T12:00:00Z");
        let hello_ack = Envelope::parse_str(concat!(
            r#"{"id":"m-r-1","kind":"hello_ack","protocol_version":0,"#,
            r#""runtime":"prop/0","ts":"2026-08-10T12:00:00Z","v":0}"#,
        ))
        .unwrap();
        session.on_envelope(&hello_ack, now);
        let mut sent_ids = Vec::new();
        for index in 0..window {
            let mut payload = Map::new();
            payload.insert("index".to_string(), Value::Number(Number::from(index)));
            let actions = session
                .send_event("prop.event", payload, None, None, "2026-08-10T12:00:01Z")
                .unwrap();
            let [Action::Send(envelope)] = actions.as_slice() else {
                panic!("send_event emits one envelope");
            };
            sent_ids.push(envelope.id().to_string());
        }
        let mut payload = Map::new();
        payload.insert("index".to_string(), Value::Number(Number::from(window)));
        let overflow =
            session.send_event("prop.event", payload, None, None, "2026-08-10T12:00:01Z");
        prop_assert!(overflow.is_err(), "the window must be a hard bound");

        let ack = Envelope::parse_str(&format!(
            concat!(
                r#"{{"event_id":"{}","id":"m-r-2","kind":"ack","#,
                r#""ts":"2026-08-10T12:00:02Z","v":0}}"#,
            ),
            sent_ids[0]
        ))
        .unwrap();
        session.on_envelope(&ack, now);
        let mut payload = Map::new();
        payload.insert("index".to_string(), Value::Number(Number::from(window + 1)));
        let freed =
            session.send_event("prop.event", payload, None, None, "2026-08-10T12:00:03Z");
        prop_assert!(freed.is_ok(), "an ack must free one window slot");
    }
}
