//! Message properties and session invariants.

use proptest::prelude::*;
use serde_json::{json, Map, Value};
use zeta_ipc::{
    Action, ErrorObject, EventTypeDecl, InitializeParams, InitializeResult, Message, MethodDecl,
    Notification, PeerIdentity, Request, RequestId, Retryability, Role, RuntimeConfig, Session,
    ShutdownDirection, SuccessResponse,
};

fn identifier() -> impl Strategy<Value = String> {
    "[a-z][a-z0-9.-]{0,19}".prop_map(|text| text)
}

fn parameter_value() -> impl Strategy<Value = Value> {
    prop_oneof![
        "[a-zA-Z0-9 é✓-]{0,24}".prop_map(Value::String),
        any::<u32>().prop_map(|number| json!(number)),
        any::<bool>().prop_map(Value::Bool),
    ]
}

fn parameters() -> impl Strategy<Value = Map<String, Value>> {
    proptest::collection::btree_map("[a-z_]{1,12}", parameter_value(), 0..6).prop_map(|entries| {
        let mut map = Map::new();
        for (key, value) in entries {
            map.insert(key, value);
        }
        map
    })
}

fn only_action(mut actions: Vec<Action>) -> Action {
    assert_eq!(actions.len(), 1, "expected exactly one action");
    actions.remove(0)
}

fn initialization_message(params: &InitializeParams, id: impl Into<RequestId>) -> Message {
    let value = serde_json::to_value(params).unwrap();
    let Value::Object(params) = value else {
        panic!("initialization parameters must serialize as an object");
    };
    Message::Request(Request::new(id.into(), "initialize", params))
}

fn response_error(actions: &[Action]) -> &ErrorObject {
    let Some(Action::Send(Message::Error(response))) = actions.first() else {
        panic!("the first action must send an error response");
    };
    &response.error
}

fn stable_error_code(error: &ErrorObject) -> Option<&str> {
    error
        .data
        .as_ref()
        .and_then(Value::as_object)
        .and_then(|data| data.get("code"))
        .and_then(Value::as_str)
}

proptest! {
    #[test]
    fn message_serialization_is_a_semantic_fixpoint(
        id in any::<i64>(),
        method in identifier(),
        params in parameters(),
    ) {
        let message = Message::Request(Request::new(RequestId::from(id), method, params));

        let encoded = message.to_json();
        let reparsed = Message::parse_str(&encoded).unwrap();

        prop_assert_eq!(reparsed, message);
    }

    #[test]
    fn a_session_never_exceeds_its_outgoing_limit(limit in 1_u64..8) {
        let params = InitializeParams {
            protocol_versions: vec![0],
            peer: PeerIdentity::new("source", "0"),
            roles: vec![Role::Source],
            event_types: Some(vec![EventTypeDecl::new("prop.event", "prop.event@1")]),
            methods: None,
            heartbeat_seconds: Some(10.0),
            max_in_flight: Some(limit),
        };
        let mut peer = Session::peer(params, ShutdownDirection::RemoteSupervisesLocal);
        let mut runtime = Session::runtime(
            RuntimeConfig::new(PeerIdentity::new("runtime", "0")),
            ShutdownDirection::LocalSupervisesRemote,
        );
        establish(&mut peer, &mut runtime);

        for index in 0..limit {
            let action = only_action(peer.send_request(
                RequestId::from(index + 10),
                "events.publish",
                publish_params(index),
            ).unwrap());
            let Action::Send(message) = action else {
                panic!("one request emits one message");
            };
            let action = only_action(runtime.receive(message));
            let Action::HandleRequest(_) = action else {
                panic!("the runtime must handle the request");
            };
        }

        let overflow = peer.send_request(
            RequestId::from(100_u64),
            "events.publish",
            publish_params(100),
        );
        prop_assert!(overflow.is_err());
        prop_assert_eq!(peer.outgoing_request_count(), limit as usize);
    }
}

fn source_provider_params() -> InitializeParams {
    InitializeParams {
        protocol_versions: vec![0],
        peer: PeerIdentity::new("connector", "0.1.0"),
        roles: vec![Role::Source, Role::Provider],
        event_types: Some(vec![EventTypeDecl::new("file.created", "file.created@1")]),
        methods: Some(vec![MethodDecl::new("file.archive")]),
        heartbeat_seconds: Some(10.0),
        max_in_flight: Some(2),
    }
}

fn establish(peer: &mut Session, runtime: &mut Session) {
    let actions = peer.initialize(RequestId::from("init")).unwrap();
    let Action::Send(request) = only_action(actions) else {
        panic!("initialize emits one request");
    };
    let Action::Send(response) = only_action(runtime.receive(request)) else {
        panic!("the runtime answers initialization");
    };
    let Action::RequestResolved(resolved) = only_action(peer.receive(response)) else {
        panic!("the peer resolves initialization");
    };
    assert_eq!(resolved.method, "initialize");
    assert!(resolved.outcome.is_ok());
    assert!(peer.is_initialized());
    assert!(runtime.is_initialized());
}

fn publish_params(index: u64) -> Map<String, Value> {
    json!({
        "type": "prop.event",
        "payload": {"index": index},
        "idempotency_key": null,
        "caused_by": null,
        "session_id": null,
        "run_id": null,
        "turn_id": null
    })
    .as_object()
    .unwrap()
    .clone()
}

#[test]
fn equal_ids_in_opposite_directions_and_out_of_order_results_do_not_collide() {
    let mut peer = Session::peer(
        source_provider_params(),
        ShutdownDirection::RemoteSupervisesLocal,
    );
    let mut runtime = Session::runtime(
        RuntimeConfig::new(PeerIdentity::new("runtime", "0.1.0")),
        ShutdownDirection::LocalSupervisesRemote,
    );
    establish(&mut peer, &mut runtime);

    let shared_id = RequestId::from(1_u64);
    let Action::Send(publish) = only_action(
        peer.send_request(
            shared_id.clone(),
            "events.publish",
            json!({
                "type": "file.created",
                "payload": {"path": "/tmp/a"},
                "idempotency_key": null,
                "caused_by": null,
                "session_id": null,
                "run_id": null,
                "turn_id": null
            })
            .as_object()
            .unwrap()
            .clone(),
        )
        .unwrap(),
    ) else {
        panic!("publish emits one request");
    };
    let Action::Send(provider_request) = only_action(
        runtime
            .send_request(
                shared_id.clone(),
                "file.archive",
                json!({"input": {"path": "/tmp/a"}, "effect_key": "effect-1"})
                    .as_object()
                    .unwrap()
                    .clone(),
            )
            .unwrap(),
    ) else {
        panic!("provider call emits one request");
    };

    let Action::HandleRequest(publish) = only_action(runtime.receive(publish)) else {
        panic!("runtime must handle publish");
    };
    let Action::HandleRequest(provider_request) = only_action(peer.receive(provider_request))
    else {
        panic!("peer must handle its declared method");
    };

    let Action::Send(provider_response) = only_action(
        peer.complete_request(&provider_request.id, json!({"archived": true}))
            .unwrap(),
    ) else {
        panic!("completion emits one response");
    };
    let Action::Send(publish_result) = only_action(
        runtime
            .complete_request(
                &publish.id,
                json!({"inserted": true, "event": durable_event(1)}),
            )
            .unwrap(),
    ) else {
        panic!("completion emits one response");
    };

    let Action::RequestResolved(resolved_provider) =
        only_action(runtime.receive(provider_response))
    else {
        panic!("runtime must resolve the provider request");
    };
    assert_eq!(resolved_provider.id, shared_id);
    assert_eq!(resolved_provider.method, "file.archive");
    let Action::RequestResolved(resolved_publish) = only_action(peer.receive(publish_result))
    else {
        panic!("peer must resolve publish");
    };
    assert_eq!(resolved_publish.id, RequestId::from(1_u64));
    assert_eq!(resolved_publish.method, "events.publish");
}

#[test]
fn direct_provider_params_support_read_only_scoped_calls() {
    let message = Message::Request(Request::new(
        RequestId::from("read-1"),
        "file.read",
        json!({
            "input": {"path": "notes.md"},
            "base_dir": "/workspace/zeta",
            "effect_key": null,
        })
        .as_object()
        .unwrap()
        .clone(),
    ));

    zeta_ipc::validate_message(&message).unwrap();
}

#[test]
fn duplicate_outgoing_ids_and_stray_responses_are_violations() {
    let mut peer = Session::peer(
        source_provider_params(),
        ShutdownDirection::RemoteSupervisesLocal,
    );
    let mut runtime = Session::runtime(
        RuntimeConfig::new(PeerIdentity::new("runtime", "0")),
        ShutdownDirection::LocalSupervisesRemote,
    );
    establish(&mut peer, &mut runtime);

    let id = RequestId::from("same");
    peer.send_request(id.clone(), "events.publish", publish_file_params())
        .unwrap();
    assert!(peer
        .send_request(id.clone(), "events.publish", publish_file_params())
        .is_err());

    let response = Message::Success(SuccessResponse::new(RequestId::from("stray"), json!({})));
    let Action::Violation(error) = only_action(peer.receive(response)) else {
        panic!("a stray response is a violation");
    };
    assert_eq!(error.code, zeta_ipc::INVALID_REQUEST);
}

#[test]
fn automatic_requests_cannot_reuse_a_pending_incoming_id() {
    let mut peer = Session::peer(
        source_provider_params(),
        ShutdownDirection::RemoteSupervisesLocal,
    );
    let mut runtime = Session::runtime(
        RuntimeConfig::new(PeerIdentity::new("runtime", "0")),
        ShutdownDirection::LocalSupervisesRemote,
    );
    establish(&mut peer, &mut runtime);

    let id = RequestId::from("shared");
    let Action::Send(publish) = only_action(
        peer.send_request(id.clone(), "events.publish", publish_file_params())
            .unwrap(),
    ) else {
        panic!("publish emits one request");
    };
    let Action::HandleRequest(publish) = only_action(runtime.receive(publish)) else {
        panic!("the runtime must hold the publish request");
    };

    let duplicate_ping = Message::Request(Request::new(id.clone(), "ping", Map::new()));
    let actions = runtime.receive(duplicate_ping);
    assert_eq!(response_error(&actions).code, zeta_ipc::INVALID_REQUEST);
    assert_eq!(runtime.incoming_request_count(), 1);

    let Action::Send(response) = only_action(
        runtime
            .complete_request(
                &publish.id,
                json!({"inserted": true, "event": durable_event(1)}),
            )
            .unwrap(),
    ) else {
        panic!("publish completion emits one response");
    };
    only_action(peer.receive(response));

    let Action::Send(ping) = only_action(peer.send_request(id, "ping", Map::new()).unwrap()) else {
        panic!("the reused id emits a ping after the first request resolves");
    };
    let Action::Send(pong) = only_action(runtime.receive(ping)) else {
        panic!("the runtime answers a reused ping id");
    };
    let Action::RequestResolved(resolved) = only_action(peer.receive(pong)) else {
        panic!("the peer resolves the ping");
    };
    assert_eq!(resolved.method, "ping");
}

#[test]
fn initialization_accepts_the_requested_role_set_in_any_order() {
    let mut peer = Session::peer(
        source_provider_params(),
        ShutdownDirection::RemoteSupervisesLocal,
    );
    only_action(peer.initialize(RequestId::from("init")).unwrap());
    let result = InitializeResult {
        protocol_version: 0,
        runtime: PeerIdentity::new("runtime", "0"),
        roles: vec![Role::Provider, Role::Source],
        config: Map::new(),
        heartbeat_seconds: 10.0,
        max_in_flight: 2,
    };
    let response = Message::Success(SuccessResponse::new(
        RequestId::from("init"),
        serde_json::to_value(result).unwrap(),
    ));

    let Action::RequestResolved(resolved) = only_action(peer.receive(response)) else {
        panic!("the peer must accept a reordered copy of its requested roles");
    };
    assert!(resolved.outcome.is_ok());
    assert!(peer.is_initialized());
}

#[test]
fn roles_gate_fixed_methods_and_declared_provider_methods() {
    let mut peer = Session::peer(
        source_provider_params(),
        ShutdownDirection::RemoteSupervisesLocal,
    );
    let mut runtime = Session::runtime(
        RuntimeConfig::new(PeerIdentity::new("runtime", "0")),
        ShutdownDirection::LocalSupervisesRemote,
    );
    establish(&mut peer, &mut runtime);

    let error = peer
        .send_request(RequestId::from(2_u64), "events.list", Map::new())
        .unwrap_err();
    assert_eq!(error.code, zeta_ipc::METHOD_NOT_FOUND);
    let error = runtime
        .send_request(
            RequestId::from(2_u64),
            "file.delete",
            json!({"input": {}, "effect_key": "effect-2"})
                .as_object()
                .unwrap()
                .clone(),
        )
        .unwrap_err();
    assert_eq!(error.code, zeta_ipc::METHOD_NOT_FOUND);
}

#[test]
fn client_event_notifications_are_delivered_without_pending_state() {
    let params = InitializeParams {
        protocol_versions: vec![0],
        peer: PeerIdentity::new("client", "0"),
        roles: vec![Role::Client],
        event_types: None,
        methods: None,
        heartbeat_seconds: None,
        max_in_flight: None,
    };
    let mut peer = Session::peer(params, ShutdownDirection::LocalSupervisesRemote);
    let mut runtime = Session::runtime(
        RuntimeConfig::new(PeerIdentity::new("runtime", "0")),
        ShutdownDirection::RemoteSupervisesLocal,
    );
    establish(&mut peer, &mut runtime);

    let Action::Send(notification) = only_action(
        runtime
            .send_notification(
                "event",
                json!({"event": durable_event(4)})
                    .as_object()
                    .unwrap()
                    .clone(),
            )
            .unwrap(),
    ) else {
        panic!("event emits one notification");
    };
    let Action::HandleNotification(Notification { method, .. }) =
        only_action(peer.receive(notification))
    else {
        panic!("the client must receive the event");
    };
    assert_eq!(method, "event");
    assert_eq!(peer.incoming_request_count(), 0);
}

#[test]
fn provider_model_observations_are_delivered_without_pending_state() {
    let mut peer = Session::peer(
        source_provider_params(),
        ShutdownDirection::RemoteSupervisesLocal,
    );
    let mut runtime = Session::runtime(
        RuntimeConfig::new(PeerIdentity::new("runtime", "0")),
        ShutdownDirection::LocalSupervisesRemote,
    );
    establish(&mut peer, &mut runtime);

    let notification = Message::Notification(Notification::new(
        "model.observation",
        json!({"observation": {"kind": "text_delta", "text": "Hello"}})
            .as_object()
            .unwrap()
            .clone(),
    ));
    let Action::HandleNotification(Notification { method, .. }) =
        only_action(runtime.receive(notification))
    else {
        panic!("the runtime must receive the model observation");
    };

    assert_eq!(method, "model.observation");
    assert_eq!(runtime.incoming_request_count(), 0);
}

#[test]
fn ping_is_automatic_and_shutdown_closes_the_managed_side() {
    let mut peer = Session::peer(
        source_provider_params(),
        ShutdownDirection::RemoteSupervisesLocal,
    );
    let mut runtime = Session::runtime(
        RuntimeConfig::new(PeerIdentity::new("runtime", "0")),
        ShutdownDirection::LocalSupervisesRemote,
    );
    establish(&mut peer, &mut runtime);

    let Action::Send(ping) = only_action(
        runtime
            .send_request(RequestId::from("ping-1"), "ping", Map::new())
            .unwrap(),
    ) else {
        panic!("ping emits one request");
    };
    let Action::Send(pong) = only_action(peer.receive(ping)) else {
        panic!("ping receives an automatic response");
    };
    let Action::RequestResolved(resolved) = only_action(runtime.receive(pong)) else {
        panic!("the ping response resolves");
    };
    assert_eq!(resolved.method, "ping");

    let Action::Send(shutdown) = only_action(
        runtime
            .send_request(
                RequestId::from("shutdown-1"),
                "shutdown",
                json!({"reason": "done"}).as_object().unwrap().clone(),
            )
            .unwrap(),
    ) else {
        panic!("shutdown emits one request");
    };
    let actions = peer.receive(shutdown);
    assert_eq!(actions.len(), 2);
    let Action::Send(Message::Success(_)) = &actions[0] else {
        panic!("shutdown must be acknowledged");
    };
    let Action::Close { reason } = &actions[1] else {
        panic!("shutdown must close the managed side");
    };
    assert_eq!(reason.as_deref(), Some("done"));
}

#[test]
fn provider_errors_resolve_with_structured_data() {
    let mut peer = Session::peer(
        source_provider_params(),
        ShutdownDirection::RemoteSupervisesLocal,
    );
    let mut runtime = Session::runtime(
        RuntimeConfig::new(PeerIdentity::new("runtime", "0")),
        ShutdownDirection::LocalSupervisesRemote,
    );
    establish(&mut peer, &mut runtime);

    let Action::Send(provider_request) = only_action(
        runtime
            .send_request(
                RequestId::from("provider-1"),
                "file.archive",
                json!({"input": {}, "effect_key": "effect-1"})
                    .as_object()
                    .unwrap()
                    .clone(),
            )
            .unwrap(),
    ) else {
        panic!("provider method emits one request");
    };
    let Action::HandleRequest(provider_request) = only_action(peer.receive(provider_request))
    else {
        panic!("provider receives its declared method");
    };
    let error = ErrorObject::application(
        -32010,
        "delivery_failed",
        "Delivery failed",
        Retryability::Retryable,
    );
    let Action::Send(response) = only_action(
        peer.fail_request(&provider_request.id, error.clone())
            .unwrap(),
    ) else {
        panic!("provider failure emits one response");
    };
    let Action::RequestResolved(resolved) = only_action(runtime.receive(response)) else {
        panic!("runtime resolves provider failure");
    };
    assert_eq!(resolved.outcome, Err(error));
}

#[test]
fn initialization_rejects_unsupported_versions_and_invalid_profiles() {
    let cases = [
        (
            InitializeParams {
                protocol_versions: vec![0],
                peer: PeerIdentity::new("duplicate-role", "0"),
                roles: vec![Role::Client, Role::Client],
                event_types: None,
                methods: None,
                heartbeat_seconds: None,
                max_in_flight: None,
            },
            zeta_ipc::INVALID_PARAMS,
        ),
        (
            InitializeParams {
                protocol_versions: vec![0],
                peer: PeerIdentity::new("source", "0"),
                roles: vec![Role::Source],
                event_types: None,
                methods: None,
                heartbeat_seconds: None,
                max_in_flight: None,
            },
            zeta_ipc::INVALID_PARAMS,
        ),
        (
            InitializeParams {
                protocol_versions: vec![0],
                peer: PeerIdentity::new("provider", "0"),
                roles: vec![Role::Provider],
                event_types: None,
                methods: Some(vec![MethodDecl::new("session.delete")]),
                heartbeat_seconds: None,
                max_in_flight: None,
            },
            zeta_ipc::INVALID_PARAMS,
        ),
    ];
    for (params, expected_code) in cases {
        let mut runtime = Session::runtime(
            RuntimeConfig::new(PeerIdentity::new("runtime", "0")),
            ShutdownDirection::LocalSupervisesRemote,
        );
        let actions = runtime.receive(initialization_message(&params, "init"));
        assert_eq!(response_error(&actions).code, expected_code);
    }

    let mut unsupported = source_provider_params();
    unsupported.protocol_versions = vec![7];
    let mut runtime = Session::runtime(
        RuntimeConfig::new(PeerIdentity::new("runtime", "0")),
        ShutdownDirection::LocalSupervisesRemote,
    );
    let actions = runtime.receive(initialization_message(&unsupported, "init"));
    assert_eq!(response_error(&actions).code, zeta_ipc::SERVER_ERROR);
    assert_eq!(
        stable_error_code(response_error(&actions)),
        Some("unsupported_version")
    );
}

#[test]
fn traffic_before_initialization_and_a_second_initialize_have_stable_codes() {
    let mut runtime = Session::runtime(
        RuntimeConfig::new(PeerIdentity::new("runtime", "0")),
        ShutdownDirection::LocalSupervisesRemote,
    );
    let request = Message::Request(Request::new(
        RequestId::from(1_u64),
        "events.list",
        Map::new(),
    ));
    let actions = runtime.receive(request);
    assert_eq!(response_error(&actions).code, zeta_ipc::INVALID_REQUEST);
    assert_eq!(
        stable_error_code(response_error(&actions)),
        Some("not_initialized")
    );

    let mut peer = Session::peer(
        source_provider_params(),
        ShutdownDirection::RemoteSupervisesLocal,
    );
    let mut runtime = Session::runtime(
        RuntimeConfig::new(PeerIdentity::new("runtime", "0")),
        ShutdownDirection::LocalSupervisesRemote,
    );
    establish(&mut peer, &mut runtime);
    let actions = runtime.receive(initialization_message(
        &source_provider_params(),
        "second-init",
    ));
    assert_eq!(response_error(&actions).code, zeta_ipc::INVALID_REQUEST);
    assert_eq!(
        stable_error_code(response_error(&actions)),
        Some("already_initialized")
    );
}

#[test]
fn incoming_requests_are_gated_even_when_the_sender_bypasses_local_checks() {
    let params = InitializeParams {
        protocol_versions: vec![0],
        peer: PeerIdentity::new("source", "0"),
        roles: vec![Role::Source],
        event_types: Some(vec![EventTypeDecl::new("file.created", "file.created@1")]),
        methods: None,
        heartbeat_seconds: None,
        max_in_flight: None,
    };
    let mut peer = Session::peer(params, ShutdownDirection::RemoteSupervisesLocal);
    let mut runtime = Session::runtime(
        RuntimeConfig::new(PeerIdentity::new("runtime", "0")),
        ShutdownDirection::LocalSupervisesRemote,
    );
    establish(&mut peer, &mut runtime);

    let request = Message::Request(Request::new(
        RequestId::from("forbidden"),
        "events.list",
        Map::new(),
    ));
    let actions = runtime.receive(request);
    assert_eq!(response_error(&actions).code, zeta_ipc::METHOD_NOT_FOUND);
}

#[test]
fn event_payload_size_is_measured_as_compact_utf8_json() {
    let fitting = "x".repeat(zeta_ipc::MAX_INLINE_PAYLOAD_BYTES - 11);
    let oversized = "x".repeat(zeta_ipc::MAX_INLINE_PAYLOAD_BYTES - 10);
    let make_message = |text: String| {
        Message::Request(Request::new(
            RequestId::from("publish"),
            "events.publish",
            json!({"type": "file.created", "payload": {"data": text}})
                .as_object()
                .unwrap()
                .clone(),
        ))
    };

    zeta_ipc::validate_message(&make_message(fitting)).unwrap();
    let error = zeta_ipc::validate_message(&make_message(oversized)).unwrap_err();
    assert_eq!(error.code, zeta_ipc::INVALID_PARAMS);
}

#[test]
fn durable_event_notifications_ignore_unknown_event_members() {
    let mut event = durable_event(8);
    event["future"] = json!({"value": true});
    let message = Message::Notification(Notification::new(
        "event",
        json!({"event": event}).as_object().unwrap().clone(),
    ));

    zeta_ipc::validate_message(&message).unwrap();
}

#[test]
fn three_idle_ticks_close_a_supervised_connection() {
    let mut peer = Session::peer(
        source_provider_params(),
        ShutdownDirection::RemoteSupervisesLocal,
    );
    let mut runtime = Session::runtime(
        RuntimeConfig::new(PeerIdentity::new("runtime", "0")),
        ShutdownDirection::LocalSupervisesRemote,
    );
    establish(&mut peer, &mut runtime);

    for index in 1..=2 {
        let action = only_action(runtime.on_tick(RequestId::from(format!("ping-{index}"))));
        let Action::Send(Message::Request(request)) = action else {
            panic!("an idle interval before the limit must send ping");
        };
        assert_eq!(request.method, "ping");
    }
    let action = only_action(runtime.on_tick(RequestId::from("ping-3")));
    let Action::Close { reason } = action else {
        panic!("three missed intervals must close the connection");
    };
    assert!(reason.unwrap().contains("three heartbeat intervals"));
}

fn publish_file_params() -> Map<String, Value> {
    json!({
        "type": "file.created",
        "payload": {"path": "/tmp/a"},
        "idempotency_key": null,
        "caused_by": null,
        "session_id": null,
        "run_id": null,
        "turn_id": null
    })
    .as_object()
    .unwrap()
    .clone()
}

fn durable_event(cursor: u64) -> Value {
    json!({
        "id": format!("evt_{cursor}"),
        "type": "file.created",
        "source": "connector",
        "payload": {},
        "idempotency_key": null,
        "caused_by": null,
        "session_id": null,
        "run_id": null,
        "turn_id": null,
        "timestamp_ms": 1,
        "cursor": cursor
    })
}
