use std::fs;
use std::path::PathBuf;
use std::str::FromStr;
use std::sync::{Arc, Barrier};
use std::thread;

use proptest::prelude::*;
use serde::Deserialize;
use serde_json::{json, Map, Value};
use tempfile::tempdir;
use zeta_dispatch::{
    attempt_id, attempt_idempotency_key, classify_attempt_failure_code, derived_run_id, effect_key,
    pending_queue_item_id, publish_event_handle, queue_item_attempt_idempotency_key, queue_item_id,
    queue_item_idempotency_key, route_event, run_id_for_attempt, safe_agent_id,
    unhandled_queue_item_id, unhandled_queue_item_idempotency_key, wait_handle, AttemptCompletion,
    AttemptCompletionDisposition, AttemptControl, AttemptFailure, AttemptFailureCode,
    AttemptStatus, CancellationFinalizationIdentities, CancellationIdentities, CancellationStatus,
    ClaimToken, DeferredPublicationStatus, Dispatch, DispatchError, EffectDeliverySemantics,
    EffectStatus, EventPattern, FailureClass, QueueClaim, QueueItemId, QueueItemStatus,
    ResourceCancellationStatus, ResourceKind, RetryPolicy, Route, RunId, RuntimeEventIdentity,
    SessionId, SessionMessageIdentities, SessionMessageRequest, SessionRule, WaitStatus,
};
use zeta_journal::{Event, EventFilter, HeadExpectation};

#[derive(Debug, Deserialize)]
struct RuntimeVectors {
    format: String,
    identity_cases: Vec<IdentityCase>,
    transitions: TransitionCases,
    retry_policies: Vec<RetryCase>,
    failure_classification: Vec<FailureCase>,
    route_cases: Vec<RouteCase>,
    session_cases: Vec<SessionCase>,
}

#[derive(Debug, Deserialize)]
struct IdentityCase {
    name: String,
    input: IdentityInput,
    expected: IdentityExpected,
}

#[derive(Debug, Deserialize)]
struct IdentityInput {
    event_id: String,
    agent_id: String,
    attempt_number: u32,
    claimed_run_id: Option<String>,
    request_position: u64,
    queue_status: String,
    attempt_status: String,
}

#[derive(Debug, Deserialize)]
struct IdentityExpected {
    safe_agent_id: String,
    pending_queue_item_id: String,
    queue_item_id: String,
    unhandled_queue_item_id: String,
    attempt_id: String,
    derived_run_id: String,
    selected_run_id: String,
    publish_event_handle: String,
    wait_handle: String,
    queue_item_idempotency_key: String,
    queue_item_attempt_idempotency_key: String,
    unhandled_queue_item_idempotency_key: String,
    attempt_idempotency_key: String,
}

#[derive(Debug, Deserialize)]
struct TransitionCases {
    queue: TransitionTable,
    attempt: TransitionTable,
}

#[derive(Debug, Deserialize)]
struct TransitionTable {
    states: Vec<String>,
    rows: Vec<TransitionRow>,
}

#[derive(Debug, Deserialize)]
struct TransitionRow {
    previous: Option<String>,
    allowed: Vec<String>,
}

#[derive(Debug, Deserialize)]
struct RetryCase {
    name: String,
    policy: RetryPolicyInput,
    attempts: Vec<RetryAttempt>,
}

#[derive(Debug, Deserialize)]
struct RetryPolicyInput {
    max_attempts: u32,
    backoff_base_seconds: f64,
    backoff_factor: f64,
    backoff_max_seconds: f64,
}

#[derive(Debug, Deserialize)]
struct RetryAttempt {
    attempt_number: u32,
    delay_seconds: f64,
    delay_ms: u64,
}

#[derive(Debug, Deserialize)]
struct FailureCase {
    error_code: String,
    failure_class: FailureClass,
}

#[derive(Debug, Deserialize)]
struct RouteCase {
    name: String,
    event: Event,
    routes: Vec<RouteInput>,
    expected_decisions: Vec<RouteDecisionExpected>,
}

#[derive(Debug, Deserialize)]
struct RouteInput {
    agent_id: String,
    accepts: Vec<String>,
    #[serde(default = "per_event_session")]
    session: String,
    #[serde(default)]
    lock_keys: Vec<String>,
    project_revision: Option<String>,
}

#[derive(Debug, Deserialize)]
struct RouteDecisionExpected {
    agent_id: String,
    queue_item_id: String,
    session_id: String,
    project_revision: Option<String>,
}

#[derive(Debug, Deserialize)]
struct SessionCase {
    name: String,
    agent_id: String,
    session: String,
    event: Event,
    expected_session_id: String,
}

fn per_event_session() -> String {
    "per-event".to_owned()
}

fn vectors() -> RuntimeVectors {
    let path =
        PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../spec/vectors/dispatch/runtime.json");
    let bytes = fs::read(&path).unwrap_or_else(|error| panic!("read {}: {error}", path.display()));
    serde_json::from_slice(&bytes)
        .unwrap_or_else(|error| panic!("decode {}: {error}", path.display()))
}

fn journal_vectors(name: &str) -> Value {
    let path = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../spec/vectors/journal")
        .join(name);
    let bytes = fs::read(&path).unwrap_or_else(|error| panic!("read {}: {error}", path.display()));
    serde_json::from_slice(&bytes)
        .unwrap_or_else(|error| panic!("decode {}: {error}", path.display()))
}

fn scripted_case(section: &str, name: &str) -> Value {
    let path =
        PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../spec/vectors/dispatch/runtime.json");
    let bytes = fs::read(&path).unwrap_or_else(|error| panic!("read {}: {error}", path.display()));
    let document: Value = serde_json::from_slice(&bytes)
        .unwrap_or_else(|error| panic!("decode {}: {error}", path.display()));
    document["scripted_cases"][section]
        .as_array()
        .unwrap()
        .iter()
        .find(|case| case["name"] == name)
        .unwrap()
        .clone()
}

fn journal_event(id: &str, key: Option<&str>) -> Event {
    Event {
        id: id.to_owned(),
        event_type: "dispatch.test".to_owned(),
        source: "rust-test".to_owned(),
        payload: serde_json::from_value(json!({"id": id})).unwrap(),
        idempotency_key: key.map(str::to_owned),
        caused_by: None,
        session_id: None,
        run_id: None,
        turn_id: None,
        timestamp_ms: 1,
        cursor: None,
    }
}

fn runtime_event(value: &Value) -> Event {
    Event {
        id: value["id"].as_str().unwrap().to_owned(),
        event_type: value["event_type"].as_str().unwrap().to_owned(),
        source: value["source"].as_str().unwrap().to_owned(),
        payload: value["payload"].as_object().unwrap().clone(),
        idempotency_key: value["idempotency_key"].as_str().map(str::to_owned),
        caused_by: value["caused_by"].as_str().map(str::to_owned),
        session_id: value["session_id"].as_str().map(str::to_owned),
        run_id: value["run_id"].as_str().map(str::to_owned),
        turn_id: value["turn_id"].as_str().map(str::to_owned),
        timestamp_ms: value["timestamp_ms"].as_i64().unwrap(),
        cursor: None,
    }
}

fn route_available_work(
    dispatch: &mut Dispatch,
    event: Event,
    agent_id: &str,
    session: SessionRule,
    lock_keys: Vec<String>,
) {
    let lifecycle_id = format!("route-{}-{agent_id}", event.id);
    dispatch.ingest_event(event.clone()).unwrap();
    let route = Route::new(
        agent_id,
        vec![EventPattern::new(event.event_type.clone())],
        session,
        lock_keys,
        Some("revision-1".to_owned()),
    );
    dispatch
        .route_ingress_event(
            &event.id,
            &[route],
            &[RuntimeEventIdentity::new(lifecycle_id, event.timestamp_ms + 1).unwrap()],
        )
        .unwrap();
}

fn route_specific_work(
    dispatch: &mut Dispatch,
    event: Event,
    agent_id: &str,
    session_id: &str,
    project_revision: Option<&str>,
) -> zeta_dispatch::QueueItemId {
    dispatch.ingest_event(event.clone()).unwrap();
    let queue_item_id = queue_item_id(&event.id, agent_id);
    let mut payload: serde_json::Map<String, Value> = serde_json::from_value(json!({
        "queue_item_id": queue_item_id,
        "event_id": event.id,
        "target_agent": agent_id,
        "session_id": session_id,
        "status": "available",
    }))
    .unwrap();
    if let Some(project_revision) = project_revision {
        payload.insert(
            "project_revision".to_owned(),
            Value::String(project_revision.to_owned()),
        );
    }
    dispatch
        .append_trusted_event(Event {
            id: format!("available-{}-{agent_id}", event.id),
            event_type: "runtime.queue_item.available".to_owned(),
            source: "zeta".to_owned(),
            payload,
            idempotency_key: Some(format!("queue_item:{}:{agent_id}:available", event.id)),
            caused_by: Some(event.id),
            session_id: Some(session_id.to_owned()),
            run_id: None,
            turn_id: event.turn_id,
            timestamp_ms: event.timestamp_ms + 1,
            cursor: None,
        })
        .unwrap();
    queue_item_id
}

fn running_completion_attempt(prefix: &str) -> (Dispatch, QueueClaim, QueueItemId) {
    let mut dispatch = Dispatch::open_in_memory().unwrap();
    let event_id = format!("evt-{prefix}");
    let session_id = format!("agent/worker/{event_id}");
    let event = Event {
        id: event_id,
        event_type: "work.requested".to_owned(),
        source: "test".to_owned(),
        payload: Map::new(),
        idempotency_key: Some(format!("ingress:{prefix}")),
        caused_by: None,
        session_id: None,
        run_id: None,
        turn_id: None,
        timestamp_ms: 100,
        cursor: None,
    };
    let queue_item_id = route_specific_work(&mut dispatch, event, "worker", &session_id, None);
    let claim = dispatch
        .claim_next_queue_item(
            "local",
            ClaimToken::new(format!("{prefix}-token")).unwrap(),
            1_000,
            200,
        )
        .unwrap()
        .unwrap();
    dispatch
        .start_claimed_attempt(
            &claim,
            201,
            RuntimeEventIdentity::new(format!("{prefix}-claimed"), 201).unwrap(),
            RuntimeEventIdentity::new(format!("{prefix}-started"), 202).unwrap(),
            "2026-08-12T10:00:00Z",
            None,
        )
        .unwrap();
    (dispatch, claim, queue_item_id)
}

#[test]
fn runtime_identity_vectors_match() {
    let vectors = vectors();
    assert_eq!(vectors.format, "zeta-dispatch-runtime-v0");

    for case in vectors.identity_cases {
        let input = case.input;
        let expected = case.expected;
        let queue_item = queue_item_id(&input.event_id, &input.agent_id);
        let attempt = attempt_id(&queue_item, input.attempt_number);

        assert_eq!(
            safe_agent_id(&input.agent_id),
            expected.safe_agent_id,
            "{}",
            case.name
        );
        assert_eq!(
            pending_queue_item_id(&input.event_id).as_str(),
            expected.pending_queue_item_id,
            "{}",
            case.name
        );
        assert_eq!(queue_item.as_str(), expected.queue_item_id, "{}", case.name);
        assert_eq!(
            unhandled_queue_item_id(&input.event_id).as_str(),
            expected.unhandled_queue_item_id,
            "{}",
            case.name
        );
        assert_eq!(attempt.as_str(), expected.attempt_id, "{}", case.name);
        assert_eq!(
            derived_run_id(&attempt).as_str(),
            expected.derived_run_id,
            "{}",
            case.name
        );
        assert_eq!(
            run_id_for_attempt(input.claimed_run_id.as_deref(), &attempt).as_str(),
            expected.selected_run_id,
            "{}",
            case.name
        );
        assert_eq!(
            publish_event_handle(&queue_item, input.request_position).as_str(),
            expected.publish_event_handle,
            "{}",
            case.name
        );
        assert_eq!(
            wait_handle(&queue_item, input.request_position).as_str(),
            expected.wait_handle,
            "{}",
            case.name
        );
        let queue_status = QueueItemStatus::from_str(&input.queue_status).unwrap();
        let attempt_status = AttemptStatus::from_str(&input.attempt_status).unwrap();
        assert_eq!(
            queue_item_idempotency_key(&input.event_id, &input.agent_id, queue_status),
            expected.queue_item_idempotency_key,
            "{}",
            case.name
        );
        assert_eq!(
            queue_item_attempt_idempotency_key(
                &input.event_id,
                &input.agent_id,
                queue_status,
                input.attempt_number
            ),
            expected.queue_item_attempt_idempotency_key,
            "{}",
            case.name
        );
        assert_eq!(
            unhandled_queue_item_idempotency_key(&input.event_id),
            expected.unhandled_queue_item_idempotency_key,
            "{}",
            case.name
        );
        assert_eq!(
            attempt_idempotency_key(&queue_item, input.attempt_number, attempt_status),
            expected.attempt_idempotency_key,
            "{}",
            case.name
        );
    }
}

#[test]
fn runtime_transition_vectors_are_exhaustive() {
    let vectors = vectors();

    assert_eq!(
        vectors.transitions.queue.states.len(),
        QueueItemStatus::ALL.len()
    );
    for status in QueueItemStatus::ALL {
        assert!(vectors
            .transitions
            .queue
            .states
            .contains(&status.to_string()));
    }
    assert_eq!(
        vectors.transitions.queue.rows.len(),
        QueueItemStatus::ALL.len() + 1
    );
    let mut queue_previous_states = Vec::new();
    for row in &vectors.transitions.queue.rows {
        let previous = row
            .previous
            .as_deref()
            .map(|previous| QueueItemStatus::from_str(previous).unwrap());
        assert!(!queue_previous_states.contains(&previous));
        queue_previous_states.push(previous);
        for current in &vectors.transitions.queue.states {
            let current_status = QueueItemStatus::from_str(current).unwrap();
            let expected = row.allowed.contains(current);
            assert_eq!(
                QueueItemStatus::can_transition(previous, current_status),
                expected
            );
        }
    }

    assert_eq!(
        vectors.transitions.attempt.states.len(),
        AttemptStatus::ALL.len()
    );
    for status in AttemptStatus::ALL {
        assert!(vectors
            .transitions
            .attempt
            .states
            .contains(&status.to_string()));
    }
    assert_eq!(
        vectors.transitions.attempt.rows.len(),
        AttemptStatus::ALL.len() + 1
    );
    let mut attempt_previous_states = Vec::new();
    for row in &vectors.transitions.attempt.rows {
        let previous = row
            .previous
            .as_deref()
            .map(|previous| AttemptStatus::from_str(previous).unwrap());
        assert!(!attempt_previous_states.contains(&previous));
        attempt_previous_states.push(previous);
        for current in &vectors.transitions.attempt.states {
            let current_status = AttemptStatus::from_str(current).unwrap();
            let expected = row.allowed.contains(current);
            assert_eq!(
                AttemptStatus::can_transition(previous, current_status),
                expected
            );
        }
    }
}

#[test]
fn runtime_retry_vectors_match() {
    let vectors = vectors();

    for case in vectors.retry_policies {
        let input = case.policy;
        let policy = RetryPolicy::new(
            input.max_attempts,
            input.backoff_base_seconds,
            input.backoff_factor,
            input.backoff_max_seconds,
        )
        .unwrap();
        for attempt in case.attempts {
            assert_eq!(
                policy.delay_seconds(attempt.attempt_number).unwrap(),
                attempt.delay_seconds,
                "{}",
                case.name
            );
            assert_eq!(
                policy.delay_ms(attempt.attempt_number).unwrap(),
                attempt.delay_ms,
                "{}",
                case.name
            );
        }
    }
}

#[test]
fn runtime_failure_classification_vectors_match() {
    let vectors = vectors();
    assert_eq!(
        vectors.failure_classification.len(),
        AttemptFailureCode::ALL.len()
    );
    let mut seen = Vec::new();
    for case in vectors.failure_classification {
        let code = AttemptFailureCode::from_str(&case.error_code).unwrap();
        assert!(!seen.contains(&code));
        seen.push(code);
        assert_eq!(classify_attempt_failure_code(code), case.failure_class);
    }
    for code in AttemptFailureCode::ALL {
        assert!(seen.contains(&code));
    }
}

#[test]
fn runtime_route_vectors_preserve_declaration_order() {
    let vectors = vectors();

    for case in vectors.route_cases {
        let mut routes = Vec::new();
        for route in case.routes {
            let patterns = route.accepts.into_iter().map(EventPattern::new).collect();
            routes.push(Route::new(
                route.agent_id,
                patterns,
                SessionRule::from_str(&route.session).unwrap(),
                route.lock_keys,
                route.project_revision,
            ));
        }
        let decisions = route_event(&case.event, &routes).unwrap();
        assert_eq!(
            decisions.len(),
            case.expected_decisions.len(),
            "{}",
            case.name
        );
        for (decision, expected) in decisions.iter().zip(case.expected_decisions) {
            assert_eq!(decision.agent_id(), expected.agent_id, "{}", case.name);
            assert_eq!(
                decision.queue_item_id().as_str(),
                expected.queue_item_id,
                "{}",
                case.name
            );
            assert_eq!(
                decision.session_id().as_str(),
                expected.session_id,
                "{}",
                case.name
            );
            assert_eq!(
                decision.project_revision(),
                expected.project_revision.as_deref(),
                "{}",
                case.name
            );
        }
    }
}

#[test]
fn route_decisions_retain_lock_declaration_order() {
    let event = Event {
        id: "evt_locks".to_owned(),
        event_type: "work.requested".to_owned(),
        source: "test".to_owned(),
        payload: serde_json::Map::new(),
        idempotency_key: None,
        caused_by: None,
        session_id: None,
        run_id: None,
        turn_id: None,
        timestamp_ms: 1,
        cursor: Some(1),
    };
    let routes = vec![Route::new(
        "worker",
        vec![EventPattern::new("work.*")],
        SessionRule::PerEvent,
        vec!["repository".to_owned(), "branch".to_owned()],
        None,
    )];

    let decisions = route_event(&event, &routes).unwrap();
    assert_eq!(decisions[0].lock_keys(), ["repository", "branch"]);
}

#[test]
fn runtime_session_vectors_match() {
    let vectors = vectors();

    for case in vectors.session_cases {
        let rule = SessionRule::from_str(&case.session).unwrap();
        let actual = rule.resolve(&case.agent_id, &case.event).unwrap();
        assert_eq!(actual.as_str(), case.expected_session_id, "{}", case.name);
    }
}

#[test]
fn session_templates_match_python_field_and_format_semantics() {
    let event = Event {
        id: "evt_format".to_owned(),
        event_type: "work.requested".to_owned(),
        source: "test".to_owned(),
        payload: serde_json::from_value(json!({
            "items": [1, 2],
            "meta": {"x": 1},
            "n": 7,
            "word": "café"
        }))
        .unwrap(),
        idempotency_key: None,
        caused_by: None,
        session_id: None,
        run_id: None,
        turn_id: None,
        timestamp_ms: 1,
        cursor: None,
    };
    let cases = [
        ("{items}", "agent/worker/[1, 2]"),
        ("{meta[x]}", "agent/worker/1"),
        ("{n:03d}", "agent/worker/007"),
        ("{n!r}", "agent/worker/7"),
        ("{word!a}", "agent/worker/'caf\\xe9'"),
        ("{event.payload[meta][x]}", "agent/worker/1"),
        ("{{literal}}/{n}", "agent/worker/{literal}/7"),
    ];

    for (template, expected) in cases {
        let rule = SessionRule::Template(template.to_owned());
        assert_eq!(rule.resolve("worker", &event).unwrap().as_str(), expected);
    }
}

#[test]
fn event_patterns_use_case_sensitive_shell_globs() {
    let event = Event {
        id: "evt_1".to_owned(),
        event_type: "github.issue.OPENED".to_owned(),
        source: "test".to_owned(),
        payload: serde_json::Map::new(),
        idempotency_key: None,
        caused_by: None,
        session_id: None,
        run_id: None,
        turn_id: None,
        timestamp_ms: 1,
        cursor: Some(1),
    };

    assert!(EventPattern::new("github.*.[A-Z][!a-z]ENED").matches(&event));
    assert!(EventPattern::new("github.issue.??????").matches(&event));
    assert!(!EventPattern::new("GITHUB.*").matches(&event));
}

proptest! {
    #[test]
    fn derived_identities_are_stable(
        event_id in "[a-zA-Z0-9_.:-]{1,40}",
        agent_id in "[a-zA-Z0-9_.:-]{1,40}",
        attempt_number in 1_u32..100,
    ) {
        let first = queue_item_id(&event_id, &agent_id);
        let second = queue_item_id(&event_id, &agent_id);
        prop_assert_eq!(&first, &second);
        prop_assert_eq!(attempt_id(&first, attempt_number), attempt_id(&second, attempt_number));
    }

    #[test]
    fn retry_delay_is_bounded(
        base in 0.0_f64..1_000.0,
        factor in 1.0_f64..5.0,
        maximum in 0.0_f64..1_000.0,
        attempt_number in 1_u32..40,
    ) {
        let policy = RetryPolicy::new(40, base, factor, maximum).unwrap();
        let delay = policy.delay_seconds(attempt_number).unwrap();
        prop_assert!(delay >= 0.0);
        prop_assert!(delay <= maximum);
    }
}

#[test]
fn sqlite_journal_replays_normative_operations() {
    let document = journal_vectors("operations.json");
    let mut dispatch = Dispatch::open_in_memory().unwrap();

    for append in document["appends"].as_array().unwrap() {
        let event: Event = serde_json::from_value(append["event"].clone()).unwrap();
        let expected = &append["expected"];
        if let Some(reason) = expected["error"].as_str() {
            let head = dispatch.head().unwrap();
            let error = dispatch.append_trusted_event(event).unwrap_err();
            let DispatchError::Append(error) = error else {
                panic!("expected append error, got {error:?}");
            };
            assert_eq!(error.reason(), reason, "{}", append["name"]);
            assert_eq!(dispatch.head().unwrap(), head, "{}", append["name"]);
            continue;
        }
        let outcome = dispatch.append_trusted_event(event).unwrap();
        assert_eq!(outcome.inserted, expected["inserted"].as_bool().unwrap());
        assert_eq!(outcome.event.id, expected["returned_id"]);
        assert_eq!(outcome.event.cursor, expected["cursor"].as_u64());
        assert_eq!(
            dispatch.head().unwrap().unwrap().to_string(),
            expected["head"].as_str().unwrap()
        );
    }

    for query in document["queries"].as_array().unwrap() {
        let filter: EventFilter = serde_json::from_value(query["filter"].clone()).unwrap();
        let events = dispatch.list_events(&filter).unwrap();
        let ids: Vec<&str> = events.iter().map(|event| event.id.as_str()).collect();
        let expected: Vec<&str> = query["expected_ids"]
            .as_array()
            .unwrap()
            .iter()
            .map(|id| id.as_str().unwrap())
            .collect();
        assert_eq!(ids, expected, "{}", query["name"]);
    }

    for chain in document["causal_chains"].as_array().unwrap() {
        let events = dispatch
            .causal_chain(chain["event_id"].as_str().unwrap())
            .unwrap();
        let ids: Vec<&str> = events.iter().map(|event| event.id.as_str()).collect();
        let expected: Vec<&str> = chain["expected_ids"]
            .as_array()
            .unwrap()
            .iter()
            .map(|id| id.as_str().unwrap())
            .collect();
        assert_eq!(ids, expected, "{}", chain["name"]);
    }

    let expected_head = document["final_head"].as_str().unwrap().parse().unwrap();
    let report = dispatch
        .verify_journal(HeadExpectation::Exact(Some(&expected_head)))
        .unwrap();
    assert_eq!(report.entries_checked, 7);
    assert_eq!(report.head, Some(expected_head));
}

#[test]
fn sqlite_journal_persists_complete_verifiable_entries() {
    let directory = tempdir().unwrap();
    let path = directory.path().join("dispatch.sqlite3");
    let expected_head = {
        let mut dispatch = Dispatch::open(&path).unwrap();
        dispatch
            .append_trusted_event(journal_event("evt_1", Some("key:1")))
            .unwrap();
        dispatch
            .append_trusted_event(journal_event("evt_2", Some("key:2")))
            .unwrap();
        dispatch.head().unwrap().unwrap()
    };

    let dispatch = Dispatch::open(&path).unwrap();
    let report = dispatch
        .verify_journal(HeadExpectation::Exact(Some(&expected_head)))
        .unwrap();
    assert_eq!(report.entries_checked, 2);
    assert_eq!(
        dispatch.get_event("evt_1").unwrap().unwrap().cursor,
        Some(1)
    );
    assert_eq!(
        dispatch.get_event("evt_2").unwrap().unwrap().cursor,
        Some(2)
    );
}

#[test]
fn sqlite_journal_rejects_legacy_and_unknown_schema_epochs() {
    let directory = tempdir().unwrap();
    let legacy_path = directory.path().join("legacy.sqlite3");
    rusqlite::Connection::open(&legacy_path)
        .unwrap()
        .execute_batch("CREATE TABLE events (id TEXT PRIMARY KEY);")
        .unwrap();
    let Err(error) = Dispatch::open(&legacy_path) else {
        panic!("legacy database opened as a new dispatch database");
    };
    assert_eq!(error.reason(), "missing_base_schema");

    let unknown_path = directory.path().join("unknown.sqlite3");
    drop(Dispatch::open(&unknown_path).unwrap());
    rusqlite::Connection::open(&unknown_path)
        .unwrap()
        .execute("UPDATE dispatch_schema SET base_epoch = 99", [])
        .unwrap();
    let Err(error) = Dispatch::open(&unknown_path) else {
        panic!("unknown schema epoch was accepted");
    };
    assert_eq!(error.reason(), "base_schema_epoch");

    let newer_projection_path = directory.path().join("newer-projection.sqlite3");
    drop(Dispatch::open(&newer_projection_path).unwrap());
    rusqlite::Connection::open(&newer_projection_path)
        .unwrap()
        .execute("UPDATE dispatch_schema SET projection_epoch = 99", [])
        .unwrap();
    let Err(error) = Dispatch::open(&newer_projection_path) else {
        panic!("newer projection epoch was accepted");
    };
    assert_eq!(error.reason(), "projection_schema_epoch");
}

#[test]
fn older_projection_epochs_drop_scheduler_state_and_rebuild_scheduled_occurrence() {
    let directory = tempdir().unwrap();
    let path = directory.path().join("older-projection.sqlite3");
    let queue_item_id = {
        let mut dispatch = Dispatch::open(&path).unwrap();
        let mut event = journal_event(
            "scheduled-occurrence",
            Some("schedule:digest:0 8 * * *:2026-08-13T08:00:00+02:00"),
        );
        event.event_type = "agent.digest.scheduled".to_owned();
        event.source = "zeta:scheduler".to_owned();
        event.payload = serde_json::from_value(json!({
            "date": "2026-08-13",
            "timestamp": "2026-08-13T08:00:00+02:00",
        }))
        .unwrap();
        route_available_work(
            &mut dispatch,
            event,
            "digest",
            SessionRule::PerEvent,
            vec!["repo:zeta".to_owned()],
        );
        let claim = dispatch
            .claim_next_queue_item(
                "worker-a",
                ClaimToken::new("epoch-token").unwrap(),
                1_000,
                100,
            )
            .unwrap()
            .unwrap();
        claim.queue_item_id().clone()
    };
    rusqlite::Connection::open(&path)
        .unwrap()
        .execute_batch(
            "CREATE TABLE IF NOT EXISTS recurring_schedules (obsolete INTEGER);
             CREATE TABLE IF NOT EXISTS scheduled_events (obsolete INTEGER);
             UPDATE dispatch_schema SET projection_epoch = 6;",
        )
        .unwrap();

    let dispatch = Dispatch::open(&path).unwrap();

    let item = dispatch.queue_item(&queue_item_id).unwrap().unwrap();
    assert_eq!(item.status(), QueueItemStatus::Available);
    assert_eq!(item.target_agent(), "digest");
    assert_eq!(item.claimed_by(), None);
    assert!(dispatch.list_locks().unwrap().is_empty());
    assert_eq!(
        dispatch
            .get_event("scheduled-occurrence")
            .unwrap()
            .unwrap()
            .event_type,
        "agent.digest.scheduled"
    );
    let obsolete_tables = rusqlite::Connection::open(&path)
        .unwrap()
        .query_row(
            "SELECT count(*) FROM sqlite_master
             WHERE type = 'table'
               AND name IN ('recurring_schedules', 'scheduled_events')",
            [],
            |row| row.get::<_, i64>(0),
        )
        .unwrap();
    assert_eq!(obsolete_tables, 0);
    assert_eq!(
        dispatch
            .verify_journal(HeadExpectation::Unanchored)
            .unwrap()
            .entries_checked,
        2
    );
}

#[test]
fn sqlite_journal_serializes_concurrent_appends() {
    let directory = tempdir().unwrap();
    let path = directory.path().join("concurrent.sqlite3");
    drop(Dispatch::open(&path).unwrap());
    let barrier = Arc::new(Barrier::new(3));
    let mut threads = Vec::new();
    for number in 1..=2 {
        let path = path.clone();
        let barrier = Arc::clone(&barrier);
        threads.push(thread::spawn(move || {
            let mut dispatch = Dispatch::open(path).unwrap();
            barrier.wait();
            dispatch
                .append_trusted_event(journal_event(&format!("evt_{number}"), None))
                .unwrap()
        }));
    }
    barrier.wait();
    for thread in threads {
        assert!(thread.join().unwrap().inserted);
    }

    let dispatch = Dispatch::open(&path).unwrap();
    let events = dispatch.list_events(&EventFilter::default()).unwrap();
    assert_eq!(events.len(), 2);
    assert_eq!(events[0].cursor, Some(1));
    assert_eq!(events[1].cursor, Some(2));
    assert_eq!(
        dispatch
            .verify_journal(HeadExpectation::Unanchored)
            .unwrap()
            .entries_checked,
        2
    );
}

#[test]
fn sqlite_journal_deduplicates_concurrent_global_keys() {
    let directory = tempdir().unwrap();
    let path = directory.path().join("duplicate.sqlite3");
    drop(Dispatch::open(&path).unwrap());
    let barrier = Arc::new(Barrier::new(3));
    let mut threads = Vec::new();
    for number in 1..=2 {
        let path = path.clone();
        let barrier = Arc::clone(&barrier);
        threads.push(thread::spawn(move || {
            let mut dispatch = Dispatch::open(path).unwrap();
            barrier.wait();
            dispatch
                .append_trusted_event(journal_event(&format!("evt_{number}"), Some("same-key")))
                .unwrap()
        }));
    }
    barrier.wait();
    let mut inserted = 0;
    let mut returned_ids = Vec::new();
    for thread in threads {
        let outcome = thread.join().unwrap();
        if outcome.inserted {
            inserted += 1;
        }
        returned_ids.push(outcome.event.id);
    }
    assert_eq!(inserted, 1);
    assert_eq!(returned_ids[0], returned_ids[1]);

    let dispatch = Dispatch::open(&path).unwrap();
    assert_eq!(
        dispatch.list_events(&EventFilter::default()).unwrap().len(),
        1
    );
}

#[test]
fn sqlite_journal_failure_does_not_advance_the_derived_head() {
    let directory = tempdir().unwrap();
    let path = directory.path().join("rollback.sqlite3");
    let expected_head = {
        let mut dispatch = Dispatch::open(&path).unwrap();
        dispatch
            .append_trusted_event(journal_event("evt_1", None))
            .unwrap();
        dispatch.head().unwrap()
    };
    rusqlite::Connection::open(&path)
        .unwrap()
        .execute_batch(
            "CREATE TRIGGER reject_injected_event
             BEFORE INSERT ON journal_entries
             WHEN NEW.event_id = 'evt_rejected'
             BEGIN SELECT RAISE(ABORT, 'injected failure'); END;",
        )
        .unwrap();
    let mut dispatch = Dispatch::open(&path).unwrap();
    let error = dispatch
        .append_trusted_event(journal_event("evt_rejected", None))
        .unwrap_err();
    assert_eq!(error.reason(), "database");
    assert_eq!(dispatch.head().unwrap(), expected_head);
    assert_eq!(
        dispatch.list_events(&EventFilter::default()).unwrap().len(),
        1
    );
}

#[test]
fn dispatch_ingress_is_idempotent_and_reserves_runtime_events() {
    let case = scripted_case(
        "ingress",
        "idempotent_external_event_and_reserved_namespace",
    );
    let draft = &case["draft"];
    let event = Event {
        id: "evt_ingress".to_owned(),
        event_type: draft["event_type"].as_str().unwrap().to_owned(),
        source: draft["source"].as_str().unwrap().to_owned(),
        payload: draft["payload"].as_object().unwrap().clone(),
        idempotency_key: draft["idempotency_key"].as_str().map(str::to_owned),
        caused_by: None,
        session_id: None,
        run_id: None,
        turn_id: None,
        timestamp_ms: 100,
        cursor: None,
    };
    let mut dispatch = Dispatch::open_in_memory().unwrap();

    let first = dispatch.ingest_event(event.clone()).unwrap();
    let repeated = dispatch.ingest_event(event).unwrap();

    assert!(first.inserted);
    assert!(!repeated.inserted);
    assert_eq!(first.event.id, repeated.event.id);
    assert_eq!(
        dispatch.list_events(&EventFilter::default()).unwrap().len(),
        1
    );
    let queue_items = dispatch.list_queue_items().unwrap();
    assert_eq!(queue_items.len(), 1);
    let pending = &queue_items[0];
    assert_eq!(pending.id().as_str(), "qi_evt_ingress");
    assert_eq!(pending.event_id(), "evt_ingress");
    assert_eq!(pending.target_agent(), "");
    assert_eq!(pending.status(), QueueItemStatus::Pending);

    let reserved = Event {
        id: "evt_reserved".to_owned(),
        event_type: case["reserved_type"].as_str().unwrap().to_owned(),
        source: "external".to_owned(),
        payload: serde_json::Map::new(),
        idempotency_key: None,
        caused_by: None,
        session_id: None,
        run_id: None,
        turn_id: None,
        timestamp_ms: 101,
        cursor: None,
    };
    let error = dispatch.ingest_event(reserved).unwrap_err();
    assert_eq!(error.reason(), "reserved_runtime_event");
    assert_eq!(
        dispatch.list_events(&EventFilter::default()).unwrap().len(),
        1
    );
}

#[test]
fn dispatch_routing_vectors_emit_atomic_lifecycle_facts() {
    for case_name in [
        "fan_out_closes_unbound_barrier",
        "unhandled_closes_unbound_barrier",
    ] {
        let case = scripted_case("routing", case_name);
        let input = runtime_event(&case["event"]);
        let mut routes = Vec::new();
        for value in case["routes"].as_array().unwrap() {
            let patterns = value["accepts"]
                .as_array()
                .unwrap()
                .iter()
                .map(|pattern| EventPattern::new(pattern.as_str().unwrap()))
                .collect();
            routes.push(Route::new(
                value["agent_id"].as_str().unwrap(),
                patterns,
                SessionRule::from_str(value["session"].as_str().unwrap()).unwrap(),
                Vec::new(),
                value["project_revision"].as_str().map(str::to_owned),
            ));
        }
        let expected_events = case["expected"]["events"].as_array().unwrap();
        let identities: Vec<RuntimeEventIdentity> = expected_events
            .iter()
            .enumerate()
            .map(|(index, expected)| {
                RuntimeEventIdentity::new(
                    format!("{case_name}-{index}"),
                    200 + i64::try_from(index).unwrap(),
                )
                .unwrap_or_else(|error| panic!("{}: {error}", expected["alias"]))
            })
            .collect();
        let mut dispatch = Dispatch::open_in_memory().unwrap();
        dispatch.ingest_event(input.clone()).unwrap();

        let outcome = dispatch
            .route_ingress_event(&input.id, &routes, &identities)
            .unwrap();

        assert_eq!(outcome.events().len(), expected_events.len(), "{case_name}");
        for (event, expected) in outcome.events().iter().zip(expected_events) {
            assert_eq!(event.event_type, expected["type"], "{case_name}");
            assert_eq!(
                event.idempotency_key.as_deref(),
                expected["idempotency_key"].as_str(),
                "{case_name}"
            );
            assert_eq!(
                event.caused_by.as_deref(),
                expected["caused_by"].as_str(),
                "{case_name}"
            );
            for (key, value) in expected["payload"].as_object().unwrap() {
                assert_eq!(event.payload.get(key), Some(value), "{case_name}: {key}");
            }
        }

        let mut queue_items = dispatch.list_queue_items().unwrap();
        queue_items.sort_by(|left, right| left.id().cmp(right.id()));
        let expected_queue_items = case["expected"]["queue_items"].as_array().unwrap();
        assert_eq!(queue_items.len(), expected_queue_items.len(), "{case_name}");
        for (item, expected) in queue_items.iter().zip(expected_queue_items) {
            assert_eq!(item.id().as_str(), expected["queue_item_id"], "{case_name}");
            assert_eq!(item.target_agent(), expected["target_agent"], "{case_name}");
            assert_eq!(
                item.session_id().map(|id| id.as_str()),
                expected["session_id"].as_str(),
                "{case_name}"
            );
            assert_eq!(
                item.project_revision(),
                expected["project_revision"].as_str(),
                "{case_name}"
            );
            assert_eq!(item.status().to_string(), expected["status"], "{case_name}");
        }
    }
}

#[test]
fn direct_route_binds_the_pending_identity_durably() {
    let event = journal_event("evt_direct", Some("ingress:direct"));
    let route = Route::new(
        "worker",
        vec![EventPattern::new("dispatch.*")],
        SessionRule::PerEvent,
        vec!["repo:zeta".to_owned()],
        Some("revision-1".to_owned()),
    );
    let identity = RuntimeEventIdentity::new("route-direct", 200).unwrap();
    let mut dispatch = Dispatch::open_in_memory().unwrap();
    dispatch.ingest_event(event.clone()).unwrap();

    let outcome = dispatch
        .route_ingress_event(&event.id, &[route], &[identity])
        .unwrap();

    assert_eq!(outcome.decisions().len(), 1);
    assert_eq!(
        outcome.decisions()[0].queue_item_id().as_str(),
        "qi_evt_direct"
    );
    assert_eq!(outcome.events().len(), 1);
    assert_eq!(
        outcome.events()[0].event_type,
        "runtime.queue_item.available"
    );
    let item = dispatch
        .queue_item(&pending_queue_item_id(&event.id))
        .unwrap()
        .unwrap();
    assert_eq!(item.target_agent(), "worker");
    assert_eq!(
        item.session_id().unwrap().as_str(),
        "agent/worker/evt_direct"
    );
    assert_eq!(item.project_revision(), Some("revision-1"));
    assert_eq!(item.status(), QueueItemStatus::Available);
}

#[test]
fn route_commit_rejects_generated_identity_collisions_atomically() {
    let event = Event {
        id: "evt_collision".to_owned(),
        event_type: "work.requested".to_owned(),
        source: "test".to_owned(),
        payload: serde_json::Map::new(),
        idempotency_key: None,
        caused_by: None,
        session_id: None,
        run_id: None,
        turn_id: None,
        timestamp_ms: 1,
        cursor: None,
    };
    let routes = vec![
        Route::new(
            "alpha",
            vec![EventPattern::new("work.*")],
            SessionRule::Shared,
            Vec::new(),
            None,
        ),
        Route::new(
            "beta",
            vec![EventPattern::new("work.*")],
            SessionRule::Shared,
            Vec::new(),
            None,
        ),
    ];
    let repeated = RuntimeEventIdentity::new("same-id", 2).unwrap();
    let mut dispatch = Dispatch::open_in_memory().unwrap();
    dispatch.ingest_event(event.clone()).unwrap();

    let error = dispatch
        .route_ingress_event(
            &event.id,
            &routes,
            &[repeated.clone(), repeated.clone(), repeated],
        )
        .unwrap_err();

    assert_eq!(error.reason(), "duplicate_runtime_event_identity");
    assert_eq!(
        dispatch.list_events(&EventFilter::default()).unwrap().len(),
        1
    );
    assert_eq!(
        dispatch
            .queue_item(&pending_queue_item_id(&event.id))
            .unwrap()
            .unwrap()
            .status(),
        QueueItemStatus::Pending
    );

    dispatch
        .append_trusted_event(Event {
            id: "collision-id".to_owned(),
            event_type: "unrelated.event".to_owned(),
            source: "test".to_owned(),
            payload: serde_json::Map::new(),
            idempotency_key: None,
            caused_by: None,
            session_id: None,
            run_id: None,
            turn_id: None,
            timestamp_ms: 3,
            cursor: None,
        })
        .unwrap();
    let identities = [
        RuntimeEventIdentity::new("collision-id", 4).unwrap(),
        RuntimeEventIdentity::new("route-alpha", 4).unwrap(),
        RuntimeEventIdentity::new("route-beta", 4).unwrap(),
    ];

    let error = dispatch
        .route_ingress_event(&event.id, &routes, &identities)
        .unwrap_err();

    assert_eq!(error.reason(), "runtime_event_identity_collision");
    assert_eq!(
        dispatch.list_events(&EventFilter::default()).unwrap().len(),
        2
    );
    assert_eq!(
        dispatch
            .queue_item(&pending_queue_item_id(&event.id))
            .unwrap()
            .unwrap()
            .status(),
        QueueItemStatus::Pending
    );
}

#[test]
fn invalid_projected_transition_rolls_back_its_journal_entry() {
    let event = journal_event("evt_transition", None);
    let mut dispatch = Dispatch::open_in_memory().unwrap();
    dispatch.ingest_event(event.clone()).unwrap();
    let identity = RuntimeEventIdentity::new("route-transition", 200).unwrap();
    let route = Route::new(
        "worker",
        vec![EventPattern::new("dispatch.*")],
        SessionRule::Shared,
        Vec::new(),
        None,
    );
    dispatch
        .route_ingress_event(&event.id, &[route], &[identity])
        .unwrap();
    let completed = Event {
        id: "queue-completed-illegal".to_owned(),
        event_type: "runtime.queue_item.completed".to_owned(),
        source: "zeta".to_owned(),
        payload: serde_json::from_value(json!({
            "queue_item_id": "qi_evt_transition",
            "event_id": "evt_transition",
            "target_agent": "worker",
            "status": "completed"
        }))
        .unwrap(),
        idempotency_key: Some("queue_item:evt_transition:worker:completed".to_owned()),
        caused_by: Some("evt_transition".to_owned()),
        session_id: Some("agent/worker".to_owned()),
        run_id: None,
        turn_id: None,
        timestamp_ms: 201,
        cursor: None,
    };

    let error = dispatch.append_trusted_event(completed).unwrap_err();

    assert_eq!(error.reason(), "queue_transition");
    assert_eq!(
        dispatch.list_events(&EventFilter::default()).unwrap().len(),
        2
    );
    assert_eq!(
        dispatch
            .queue_item(&pending_queue_item_id(&event.id))
            .unwrap()
            .unwrap()
            .status(),
        QueueItemStatus::Available
    );
}

#[test]
fn projection_rebuild_preserves_running_attempt_and_releases_claimed_item() {
    let case = scripted_case(
        "projection_recovery",
        "rebuild_preserves_history_and_releases_claim",
    );
    let mut dispatch = Dispatch::open_in_memory().unwrap();
    for value in case["events"].as_array().unwrap() {
        let event = runtime_event(value);
        dispatch.ingest_event(event).unwrap();
    }
    for value in case["lifecycle_events"].as_array().unwrap() {
        let event = runtime_event(value);
        dispatch.append_trusted_event(event).unwrap();
    }

    let replayed = dispatch.rebuild_projections().unwrap();

    assert_eq!(replayed, 3);
    let item = dispatch
        .queue_item(&pending_queue_item_id("evt_recover"))
        .unwrap()
        .unwrap();
    assert_eq!(item.status(), QueueItemStatus::Available);
    assert_eq!(item.target_agent(), "worker");
    assert_eq!(item.claimed_by(), None);
    assert_eq!(item.claimed_until(), None);
    let attempts = dispatch.list_attempts().unwrap();
    assert_eq!(attempts.len(), 1);
    assert_eq!(attempts[0].status(), AttemptStatus::Running);
    assert_eq!(attempts[0].id().as_str(), "att_qi_evt_recover_1");
    assert_eq!(attempts[0].queue_item_id().as_str(), "qi_evt_recover");
    assert_eq!(attempts[0].attempt_number(), 1);
    let event_ids: Vec<String> = dispatch
        .list_events(&EventFilter::default())
        .unwrap()
        .into_iter()
        .map(|event| event.id)
        .collect();
    assert_eq!(
        event_ids,
        case["expected"]["journal_event_ids"]
            .as_array()
            .unwrap()
            .iter()
            .map(|value| value.as_str().unwrap().to_owned())
            .collect::<Vec<_>>()
    );
}

#[test]
fn unrouted_ingress_events_recover_a_stalled_queue_after_restart() {
    let mut dispatch = Dispatch::open_in_memory().unwrap();
    let stalled = journal_event("evt_stalled", None);
    let routed = Event {
        timestamp_ms: 2,
        ..journal_event("evt_routed", None)
    };
    dispatch.ingest_event(stalled.clone()).unwrap();
    dispatch.ingest_event(routed.clone()).unwrap();

    let route = Route::new(
        "worker",
        vec![EventPattern::new("dispatch.test")],
        SessionRule::PerEvent,
        Vec::new(),
        None,
    );
    dispatch
        .route_ingress_event(
            &routed.id,
            std::slice::from_ref(&route),
            &[RuntimeEventIdentity::new("route-evt_routed", 3).unwrap()],
        )
        .unwrap();

    let token = ClaimToken::new("recovery-token").unwrap();
    let blocked = dispatch
        .claim_next_queue_item("local", token.clone(), 1_000, 10)
        .unwrap();
    assert!(blocked.is_none());
    assert_eq!(dispatch.unrouted_ingress_events().unwrap(), ["evt_stalled"]);

    dispatch
        .route_ingress_event(
            &stalled.id,
            &[route],
            &[RuntimeEventIdentity::new("route-evt_stalled", 4).unwrap()],
        )
        .unwrap();
    assert!(dispatch.unrouted_ingress_events().unwrap().is_empty());

    let claim = dispatch
        .claim_next_queue_item("local", token, 1_000, 10)
        .unwrap()
        .unwrap();
    assert_eq!(
        claim.queue_item_id().as_str(),
        pending_queue_item_id(&stalled.id).as_str()
    );
}

#[test]
fn ingress_projection_failure_rolls_back_the_journal_append() {
    let directory = tempdir().unwrap();
    let path = directory.path().join("projection-rollback.sqlite3");
    drop(Dispatch::open(&path).unwrap());
    rusqlite::Connection::open(&path)
        .unwrap()
        .execute_batch(
            "CREATE TRIGGER reject_pending_projection
             BEFORE INSERT ON queue_items
             BEGIN SELECT RAISE(ABORT, 'injected projection failure'); END;",
        )
        .unwrap();
    let mut dispatch = Dispatch::open(&path).unwrap();

    let error = dispatch
        .ingest_event(journal_event("evt_projection_rollback", None))
        .unwrap_err();

    assert_eq!(error.reason(), "database");
    assert_eq!(dispatch.head().unwrap(), None);
    assert!(dispatch
        .list_events(&EventFilter::default())
        .unwrap()
        .is_empty());
    assert!(dispatch.list_queue_items().unwrap().is_empty());
}

#[test]
fn claim_fencing_vector_keeps_released_tokens_stale() {
    let case = scripted_case("claim_fencing", "released_token_stays_stale");
    let event = runtime_event(&case["event"]);
    let lease_ms = case["lease_ms"].as_u64().unwrap();
    let now_ms = case["now_ms"].as_i64().unwrap();
    let workers = case["workers"].as_array().unwrap();
    let mut dispatch = Dispatch::open_in_memory().unwrap();
    route_available_work(
        &mut dispatch,
        event,
        "worker",
        SessionRule::PerEvent,
        Vec::new(),
    );
    let token_a = ClaimToken::new("claim-token-a").unwrap();
    let token_b = ClaimToken::new("claim-token-b").unwrap();

    let first = dispatch
        .claim_next_queue_item(
            workers[0].as_str().unwrap(),
            token_a.clone(),
            lease_ms,
            now_ms,
        )
        .unwrap()
        .unwrap();
    let wrong = first.with_token(ClaimToken::new("wrong-token").unwrap());
    assert!(!dispatch.claim_is_current(&wrong, now_ms).unwrap());
    assert!(dispatch.release_claim(&first, now_ms + 1).unwrap());
    assert!(!dispatch.claim_is_current(&first, now_ms + 2).unwrap());
    let second = dispatch
        .claim_next_queue_item(workers[1].as_str().unwrap(), token_b, lease_ms, now_ms + 2)
        .unwrap()
        .unwrap();

    assert_ne!(first.token(), second.token());
    assert!(dispatch.claim_is_current(&second, now_ms + 2).unwrap());
    assert_eq!(
        dispatch
            .queue_item(second.queue_item_id())
            .unwrap()
            .unwrap()
            .status()
            .to_string(),
        case["expected"]["queue_status"]
    );
}

#[test]
fn two_dispatchers_have_one_claim_winner() {
    let directory = tempdir().unwrap();
    let path = directory.path().join("claim-race.sqlite3");
    let event = journal_event("evt_claim_race", Some("ingress:claim-race"));
    let mut setup = Dispatch::open(&path).unwrap();
    route_available_work(
        &mut setup,
        event,
        "worker",
        SessionRule::PerEvent,
        Vec::new(),
    );
    drop(setup);
    let barrier = Arc::new(Barrier::new(3));
    let mut threads = Vec::new();
    for number in 1..=2 {
        let path = path.clone();
        let barrier = Arc::clone(&barrier);
        threads.push(thread::spawn(move || {
            let mut dispatch = Dispatch::open(path).unwrap();
            barrier.wait();
            dispatch
                .claim_next_queue_item(
                    &format!("worker-{number}"),
                    ClaimToken::new(format!("token-{number}")).unwrap(),
                    1_000,
                    100,
                )
                .unwrap()
        }));
    }
    barrier.wait();
    let mut winners = Vec::new();
    for thread in threads {
        if let Some(claim) = thread.join().unwrap() {
            winners.push(claim);
        }
    }
    assert_eq!(winners.len(), 1);
}

#[test]
fn claims_acquire_all_locks_and_respect_session_order() {
    let mut dispatch = Dispatch::open_in_memory().unwrap();
    let mut first = journal_event("evt_lock_first", Some("ingress:lock-first"));
    first.event_type = "work.requested".to_owned();
    first.timestamp_ms = 10;
    route_available_work(
        &mut dispatch,
        first,
        "worker",
        SessionRule::Shared,
        vec!["repo:zeta".to_owned(), "branch:main".to_owned()],
    );
    let mut same_session = journal_event("evt_lock_second", Some("ingress:lock-second"));
    same_session.event_type = "work.requested".to_owned();
    same_session.timestamp_ms = 20;
    route_available_work(
        &mut dispatch,
        same_session,
        "worker",
        SessionRule::Shared,
        vec!["repo:other".to_owned()],
    );
    let mut conflicting = journal_event("evt_lock_third", Some("ingress:lock-third"));
    conflicting.event_type = "work.requested".to_owned();
    conflicting.timestamp_ms = 30;
    route_available_work(
        &mut dispatch,
        conflicting,
        "other",
        SessionRule::PerEvent,
        vec!["repo:zeta".to_owned()],
    );

    let first_claim = dispatch
        .claim_next_queue_item(
            "worker-a",
            ClaimToken::new("token-first").unwrap(),
            1_000,
            100,
        )
        .unwrap()
        .unwrap();
    assert_eq!(first_claim.queue_item_id().as_str(), "qi_evt_lock_first");
    let locks = dispatch.list_locks().unwrap();
    assert_eq!(locks.len(), 2);
    assert_eq!(locks[0].key(), "branch:main");
    assert_eq!(locks[1].key(), "repo:zeta");
    assert!(dispatch
        .claim_next_queue_item(
            "worker-b",
            ClaimToken::new("token-blocked").unwrap(),
            1_000,
            100,
        )
        .unwrap()
        .is_none());

    assert!(dispatch.release_claim(&first_claim, 101).unwrap());
    assert!(dispatch.list_locks().unwrap().is_empty());
    let reclaimed = dispatch
        .claim_next_queue_item(
            "worker-b",
            ClaimToken::new("token-reclaimed").unwrap(),
            1_000,
            102,
        )
        .unwrap()
        .unwrap();
    assert_eq!(reclaimed.queue_item_id().as_str(), "qi_evt_lock_first");
}

#[test]
fn an_unbound_barrier_blocks_later_claims() {
    let mut dispatch = Dispatch::open_in_memory().unwrap();
    let mut barrier = journal_event("evt_barrier", Some("ingress:barrier"));
    barrier.event_type = "work.requested".to_owned();
    barrier.timestamp_ms = 10;
    dispatch.ingest_event(barrier).unwrap();
    let mut later = journal_event("evt_later", Some("ingress:later"));
    later.event_type = "work.requested".to_owned();
    later.timestamp_ms = 20;
    route_available_work(
        &mut dispatch,
        later,
        "worker",
        SessionRule::PerEvent,
        Vec::new(),
    );

    let claim = dispatch
        .claim_next_queue_item(
            "worker",
            ClaimToken::new("token-later").unwrap(),
            1_000,
            100,
        )
        .unwrap();

    assert!(claim.is_none());
}

#[test]
fn renew_and_expiry_fence_claims_and_locks() {
    let mut dispatch = Dispatch::open_in_memory().unwrap();
    let mut event = journal_event("evt_lease", Some("ingress:lease"));
    event.event_type = "work.requested".to_owned();
    route_available_work(
        &mut dispatch,
        event,
        "worker",
        SessionRule::PerEvent,
        vec!["repo:zeta".to_owned()],
    );
    let claim = dispatch
        .claim_next_queue_item(
            "worker-a",
            ClaimToken::new("lease-token").unwrap(),
            100,
            1_000,
        )
        .unwrap()
        .unwrap();
    assert!(dispatch.claim_is_current(&claim, 1_099).unwrap());
    assert!(!dispatch.claim_is_current(&claim, 1_100).unwrap());
    assert!(dispatch.renew_claim(&claim, 200, 1_050).unwrap());
    assert!(dispatch.claim_is_current(&claim, 1_249).unwrap());
    assert_eq!(dispatch.list_locks().unwrap()[0].expires_at(), 1_250);
    assert_eq!(dispatch.reconcile_expired_claims(1_250).unwrap(), 1);
    assert!(!dispatch.claim_is_current(&claim, 1_250).unwrap());
    assert!(dispatch.list_locks().unwrap().is_empty());
}

#[test]
fn fenced_attempt_start_commits_claimed_and_running_facts() {
    let mut dispatch = Dispatch::open_in_memory().unwrap();
    let mut event = journal_event("evt_attempt", Some("ingress:attempt"));
    event.event_type = "work.requested".to_owned();
    route_available_work(
        &mut dispatch,
        event,
        "worker",
        SessionRule::PerEvent,
        vec!["repo:zeta".to_owned()],
    );
    let claim = dispatch
        .claim_next_queue_item(
            "worker-a",
            ClaimToken::new("attempt-token").unwrap(),
            1_000,
            100,
        )
        .unwrap()
        .unwrap();
    let started = dispatch
        .start_claimed_attempt(
            &claim,
            101,
            RuntimeEventIdentity::new("queue-claimed", 101).unwrap(),
            RuntimeEventIdentity::new("attempt-started", 102).unwrap(),
            "2026-08-12T10:00:00Z",
            None,
        )
        .unwrap();

    assert_eq!(started.attempt().id().as_str(), "att_qi_evt_attempt_1");
    assert_eq!(
        started.attempt().run_id().unwrap().as_str(),
        "run_att_qi_evt_attempt_1"
    );
    assert_eq!(started.events().len(), 2);
    assert_eq!(started.events()[0].event_type, "runtime.queue_item.claimed");
    assert_eq!(
        started.events()[0].idempotency_key.as_deref(),
        Some("queue_item:evt_attempt:worker:claimed:1")
    );
    assert_eq!(started.events()[1].event_type, "runtime.attempt.started");
    assert_eq!(
        started.events()[1].idempotency_key.as_deref(),
        Some("attempt:qi_evt_attempt:1:started")
    );
    assert_eq!(
        dispatch
            .queue_item(claim.queue_item_id())
            .unwrap()
            .unwrap()
            .status(),
        QueueItemStatus::Claimed
    );
    assert_eq!(dispatch.list_attempts().unwrap().len(), 1);

    let stale = claim.with_token(ClaimToken::new("stale-token").unwrap());
    let error = dispatch
        .start_claimed_attempt(
            &stale,
            103,
            RuntimeEventIdentity::new("stale-claimed", 103).unwrap(),
            RuntimeEventIdentity::new("stale-started", 104).unwrap(),
            "2026-08-12T10:00:01Z",
            None,
        )
        .unwrap_err();
    assert_eq!(error.reason(), "claim_not_current");
    assert_eq!(dispatch.list_attempts().unwrap().len(), 1);
}

#[test]
fn repeating_attempt_start_returns_the_retained_attempt() {
    let mut dispatch = Dispatch::open_in_memory().unwrap();
    let mut event = journal_event("evt_start_retry", Some("ingress:start-retry"));
    event.event_type = "work.requested".to_owned();
    route_available_work(
        &mut dispatch,
        event,
        "worker",
        SessionRule::PerEvent,
        Vec::new(),
    );
    let claim = dispatch
        .claim_next_queue_item(
            "worker-a",
            ClaimToken::new("start-retry-token").unwrap(),
            1_000,
            100,
        )
        .unwrap()
        .unwrap();
    let first = dispatch
        .start_claimed_attempt(
            &claim,
            101,
            RuntimeEventIdentity::new("start-retry-claimed-1", 101).unwrap(),
            RuntimeEventIdentity::new("start-retry-attempt-1", 102).unwrap(),
            "2026-08-12T10:00:00Z",
            None,
        )
        .unwrap();

    let repeated = dispatch
        .start_claimed_attempt(
            &claim,
            103,
            RuntimeEventIdentity::new("start-retry-claimed-2", 103).unwrap(),
            RuntimeEventIdentity::new("start-retry-attempt-2", 104).unwrap(),
            "2026-08-12T10:00:00Z",
            None,
        )
        .unwrap();

    assert_eq!(repeated.attempt(), first.attempt());
    assert_eq!(repeated.events(), first.events());
    assert_eq!(dispatch.list_attempts().unwrap().len(), 1);
    assert_eq!(
        dispatch
            .queue_item(claim.queue_item_id())
            .unwrap()
            .unwrap()
            .attempt_count(),
        1
    );
}

#[test]
fn failed_attempts_retry_then_dead_letter_from_vector() {
    let case = scripted_case("attempt_outcomes", "retry_then_dead_letter");
    let event = runtime_event(&case["triggering_event"]);
    let queue = &case["queue_item"];
    let mut dispatch = Dispatch::open_in_memory().unwrap();
    dispatch.ingest_event(event.clone()).unwrap();
    dispatch
        .append_trusted_event(Event {
            id: "retry-route".to_owned(),
            event_type: "runtime.queue_item.available".to_owned(),
            source: "zeta".to_owned(),
            payload: serde_json::from_value(json!({
                "queue_item_id": queue["queue_item_id"],
                "event_id": queue["event_id"],
                "target_agent": queue["target_agent"],
                "status": "available",
                "session_id": queue["session_id"],
            }))
            .unwrap(),
            idempotency_key: Some("queue_item:evt_retry:worker:available".to_owned()),
            caused_by: Some(event.id.clone()),
            session_id: queue["session_id"].as_str().map(str::to_owned),
            run_id: None,
            turn_id: None,
            timestamp_ms: 101,
            cursor: None,
        })
        .unwrap();
    let policy_value = &case["retry_policy"];
    let policy = RetryPolicy::new(
        u32::try_from(policy_value["max_attempts"].as_u64().unwrap()).unwrap(),
        policy_value["backoff_base_seconds"].as_f64().unwrap(),
        policy_value["backoff_factor"].as_f64().unwrap(),
        policy_value["backoff_max_seconds"].as_f64().unwrap(),
    )
    .unwrap();
    let expected = case["expected"]["events"].as_array().unwrap();
    let mut observed = Vec::new();

    for attempt_number in 1..=2 {
        let claim_now = 100 + 100 * i64::from(attempt_number);
        let claim = dispatch
            .claim_next_queue_item(
                "local",
                ClaimToken::new(format!("retry-token-{attempt_number}")).unwrap(),
                1_000,
                claim_now,
            )
            .unwrap()
            .unwrap();
        let started = dispatch
            .start_claimed_attempt(
                &claim,
                claim_now + 1,
                RuntimeEventIdentity::new(format!("retry-claimed-{attempt_number}"), claim_now + 1)
                    .unwrap(),
                RuntimeEventIdentity::new(format!("retry-started-{attempt_number}"), claim_now + 2)
                    .unwrap(),
                &format!("2026-08-12T10:00:0{attempt_number}Z"),
                None,
            )
            .unwrap();
        observed.extend_from_slice(started.events());
        observed.extend(
            dispatch
                .fail_claimed_attempt(
                    &claim,
                    claim_now + 3,
                    [
                        RuntimeEventIdentity::new(
                            format!("retry-failed-{attempt_number}"),
                            claim_now + 3,
                        )
                        .unwrap(),
                        RuntimeEventIdentity::new(
                            format!("retry-disposition-{attempt_number}"),
                            claim_now + 4,
                        )
                        .unwrap(),
                    ],
                    &AttemptFailure::new(
                        format!("2026-08-12T10:01:0{attempt_number}Z"),
                        case["failure_message"].as_str().unwrap(),
                        AttemptFailureCode::AgentExecutionFailed,
                        policy,
                    ),
                )
                .unwrap(),
        );
    }

    assert_eq!(observed.len(), expected.len());
    for (actual, expected) in observed.iter().zip(expected) {
        assert_eq!(actual.event_type, expected["type"]);
        assert_eq!(
            actual.idempotency_key.as_deref(),
            expected["idempotency_key"].as_str()
        );
        assert_eq!(actual.caused_by.as_deref(), expected["caused_by"].as_str());
        for (key, value) in expected["payload"].as_object().unwrap() {
            assert_eq!(actual.payload.get(key), Some(value), "payload field {key}");
        }
    }
    let queue_item_id = queue_item_id("evt_retry", "worker");
    let queue_item = dispatch.queue_item(&queue_item_id).unwrap().unwrap();
    assert_eq!(queue_item.status(), QueueItemStatus::DeadLettered);
    assert_eq!(queue_item.attempt_count(), 2);
    assert_eq!(queue_item.claimed_by(), None);
    assert!(dispatch.list_locks().unwrap().is_empty());
    let attempts = dispatch.list_attempts().unwrap();
    assert_eq!(attempts.len(), 2);
    assert!(attempts
        .iter()
        .all(|attempt| attempt.status() == AttemptStatus::Failed));
}

#[test]
fn permanent_attempt_failure_dead_letters_without_retry() {
    let mut dispatch = Dispatch::open_in_memory().unwrap();
    let mut event = journal_event("evt_permanent", Some("ingress:permanent"));
    event.event_type = "work.requested".to_owned();
    route_available_work(
        &mut dispatch,
        event,
        "worker",
        SessionRule::PerEvent,
        vec!["repo:zeta".to_owned()],
    );
    let claim = dispatch
        .claim_next_queue_item(
            "worker-a",
            ClaimToken::new("permanent-token").unwrap(),
            1_000,
            100,
        )
        .unwrap()
        .unwrap();
    dispatch
        .start_claimed_attempt(
            &claim,
            101,
            RuntimeEventIdentity::new("permanent-claimed", 101).unwrap(),
            RuntimeEventIdentity::new("permanent-started", 102).unwrap(),
            "2026-08-12T10:00:00Z",
            None,
        )
        .unwrap();

    let events = dispatch
        .fail_claimed_attempt(
            &claim,
            103,
            [
                RuntimeEventIdentity::new("permanent-failed", 103).unwrap(),
                RuntimeEventIdentity::new("permanent-dead", 104).unwrap(),
            ],
            &AttemptFailure::new(
                "2026-08-12T10:00:01Z",
                "invalid result",
                AttemptFailureCode::MalformedEventPayload,
                RetryPolicy::default(),
            ),
        )
        .unwrap();

    assert_eq!(events[0].event_type, "runtime.attempt.failed");
    assert_eq!(events[1].event_type, "runtime.queue_item.dead_lettered");
    assert_eq!(events[1].payload["reason"], "permanent");
    assert!(!dispatch.claim_is_current(&claim, 103).unwrap());
    assert_eq!(dispatch.list_locks().unwrap(), Vec::new());
}

#[test]
fn queued_cancellation_commits_intent_and_terminal_fact_once() {
    let case = scripted_case("cancellation", "queued_turn_cancels_once");
    let mut dispatch = Dispatch::open_in_memory().unwrap();
    let message = Event {
        id: "evt_cancel_message".to_owned(),
        event_type: "session.message.requested".to_owned(),
        source: "test".to_owned(),
        payload: serde_json::from_value(json!({
            "message": case["message"]["message"],
            "agent_id": case["message"]["agent_id"],
            "session_id": case["message"]["session_id"],
        }))
        .unwrap(),
        idempotency_key: Some("session.message:session-1:cancel-turn".to_owned()),
        caused_by: None,
        session_id: Some("session-1".to_owned()),
        run_id: Some("run-cancel".to_owned()),
        turn_id: None,
        timestamp_ms: 900,
        cursor: None,
    };
    dispatch.ingest_event(message.clone()).unwrap();
    let queue_item_id = queue_item_id(&message.id, "master");
    dispatch
        .append_trusted_event(Event {
            id: "cancel-available".to_owned(),
            event_type: "runtime.queue_item.available".to_owned(),
            source: "zeta".to_owned(),
            payload: serde_json::from_value(json!({
                "queue_item_id": queue_item_id,
                "event_id": message.id,
                "target_agent": "master",
                "project_revision": "revision-1",
                "session_id": "session-1",
                "status": "available",
            }))
            .unwrap(),
            idempotency_key: Some("queue_item:evt_cancel_message:master:available".to_owned()),
            caused_by: Some(message.id.clone()),
            session_id: Some("session-1".to_owned()),
            run_id: Some("run-cancel".to_owned()),
            turn_id: None,
            timestamp_ms: 901,
            cursor: None,
        })
        .unwrap();
    let identities = CancellationIdentities::new(
        RuntimeEventIdentity::new("cancel-requested", 1_000).unwrap(),
        RuntimeEventIdentity::new("cancelled", 1_000).unwrap(),
    );

    let outcome = dispatch
        .cancel_queue_item(
            &queue_item_id,
            Some("session-1"),
            case["reason"].as_str(),
            identities,
        )
        .unwrap();

    assert_eq!(outcome.status(), CancellationStatus::Cancelled);
    assert!(outcome.changed());
    assert_eq!(outcome.events().len(), 2);
    let expected = &case["expected"]["events"].as_array().unwrap()[2..];
    for (actual, expected) in outcome.events().iter().zip(expected) {
        assert_eq!(actual.event_type, expected["type"]);
        let expected_key = expected["idempotency_key"]
            .as_str()
            .unwrap()
            .replace("$message", &message.id);
        assert_eq!(
            actual.idempotency_key.as_deref(),
            Some(expected_key.as_str())
        );
        for (key, value) in expected["payload"].as_object().unwrap() {
            let value = if value == "$message" {
                Value::String(message.id.clone())
            } else if value == "qi_$message_master" {
                Value::String(queue_item_id.to_string())
            } else {
                value.clone()
            };
            assert_eq!(actual.payload.get(key), Some(&value), "payload field {key}");
        }
    }
    assert_eq!(
        outcome.events()[1].caused_by.as_deref(),
        Some("cancel-requested")
    );
    let item = dispatch.queue_item(&queue_item_id).unwrap().unwrap();
    assert_eq!(item.status(), QueueItemStatus::Cancelled);
    assert_eq!(
        item.cancellation_requested_event_id(),
        Some("cancel-requested")
    );
    assert_eq!(item.cancellation_requested_at(), Some(1_000));
    assert_eq!(item.cancellation_reason(), case["reason"].as_str());

    let repeated = dispatch
        .cancel_queue_item(
            &queue_item_id,
            Some("session-1"),
            Some("later reason"),
            CancellationIdentities::new(
                RuntimeEventIdentity::new("cancel-requested-retry", 2_000).unwrap(),
                RuntimeEventIdentity::new("cancelled-retry", 2_000).unwrap(),
            ),
        )
        .unwrap();
    assert_eq!(repeated.status(), CancellationStatus::AlreadyCancelled);
    assert!(!repeated.changed());
    assert_eq!(
        dispatch
            .list_events(&EventFilter {
                event_type: Some("runtime.queue_item.cancel_requested".to_owned()),
                ..EventFilter::default()
            })
            .unwrap()
            .len(),
        1
    );
}

#[test]
fn public_run_cancellation_resolves_the_stable_queue_item() {
    let mut dispatch = Dispatch::open_in_memory().unwrap();
    let mut event = journal_event("evt_public_run", Some("ingress:public-run"));
    event.event_type = "work.requested".to_owned();
    event.run_id = Some("run-public".to_owned());
    route_available_work(
        &mut dispatch,
        event,
        "worker",
        SessionRule::PerEvent,
        Vec::new(),
    );

    let outcome = dispatch
        .cancel_run(
            &RunId::from_str("run-public").unwrap(),
            Some("agent/worker/evt_public_run"),
            Some("user stopped the run"),
            CancellationIdentities::new(
                RuntimeEventIdentity::new("public-run-cancel-request", 500).unwrap(),
                RuntimeEventIdentity::new("public-run-cancelled", 501).unwrap(),
            ),
        )
        .unwrap();
    assert_eq!(outcome.status(), CancellationStatus::Cancelled);
    assert_eq!(
        outcome.queue_item_id().unwrap().as_str(),
        "qi_evt_public_run"
    );
    assert_eq!(outcome.events()[0].run_id.as_deref(), Some("run-public"));

    let unknown = dispatch
        .cancel_run(
            &RunId::from_str("run-unknown").unwrap(),
            None,
            None,
            CancellationIdentities::new(
                RuntimeEventIdentity::new("unknown-run-request", 600).unwrap(),
                RuntimeEventIdentity::new("unknown-run-cancelled", 601).unwrap(),
            ),
        )
        .unwrap();
    assert_eq!(unknown.status(), CancellationStatus::Unknown);
    assert!(unknown.queue_item_id().is_none());
    assert!(!unknown.changed());
}

#[test]
fn cancellation_wins_inside_a_fenced_failure_transaction() {
    let mut dispatch = Dispatch::open_in_memory().unwrap();
    let mut event = journal_event("evt_cancel_running", Some("ingress:cancel-running"));
    event.event_type = "work.requested".to_owned();
    event.turn_id = Some("turn-running".to_owned());
    route_available_work(
        &mut dispatch,
        event,
        "worker",
        SessionRule::PerEvent,
        vec!["repo:zeta".to_owned()],
    );
    let claim = dispatch
        .claim_next_queue_item(
            "worker-a",
            ClaimToken::new("cancel-running-token").unwrap(),
            1_000,
            100,
        )
        .unwrap()
        .unwrap();

    let cancellation = dispatch
        .cancel_queue_item(
            claim.queue_item_id(),
            Some("agent/worker/evt_cancel_running"),
            Some("changed direction"),
            CancellationIdentities::new(
                RuntimeEventIdentity::new("running-cancel-request", 101).unwrap(),
                RuntimeEventIdentity::new("unused-running-cancel", 101).unwrap(),
            ),
        )
        .unwrap();

    assert_eq!(cancellation.status(), CancellationStatus::Cancelling);
    assert_eq!(cancellation.events().len(), 1);
    assert_eq!(cancellation.events()[0].payload["status"], "claimed");
    assert!(dispatch.claim_is_current(&claim, 102).unwrap());
    let started = dispatch
        .start_claimed_attempt(
            &claim,
            102,
            RuntimeEventIdentity::new("running-claimed", 102).unwrap(),
            RuntimeEventIdentity::new("running-started", 103).unwrap(),
            "2026-08-12T10:00:00Z",
            None,
        )
        .unwrap();
    assert_eq!(started.attempt().status(), AttemptStatus::Running);

    let terminal = dispatch
        .fail_claimed_attempt(
            &claim,
            104,
            [
                RuntimeEventIdentity::new("running-attempt-cancelled", 104).unwrap(),
                RuntimeEventIdentity::new("running-queue-cancelled", 105).unwrap(),
            ],
            &AttemptFailure::new(
                "2026-08-12T10:00:01Z",
                "this failure must not retry",
                AttemptFailureCode::NetworkError,
                RetryPolicy::default(),
            ),
        )
        .unwrap();

    assert_eq!(terminal[0].event_type, "runtime.attempt.cancelled");
    assert_eq!(terminal[1].event_type, "runtime.queue_item.cancelled");
    assert_eq!(terminal[0].caused_by.as_deref(), Some("evt_cancel_running"));
    assert_eq!(terminal[1].caused_by.as_deref(), Some("evt_cancel_running"));
    assert_eq!(terminal[0].turn_id.as_deref(), Some("turn-running"));
    assert_eq!(terminal[0].payload["result"]["outcome"], "cancelled");
    assert_eq!(terminal[0].payload["result"]["stop_reason"], "aborted");
    assert!(terminal[1].payload.get("reason").is_none());
    assert_eq!(
        dispatch
            .queue_item(claim.queue_item_id())
            .unwrap()
            .unwrap()
            .status(),
        QueueItemStatus::Cancelled
    );
    assert_eq!(
        dispatch.list_attempts().unwrap()[0].status(),
        AttemptStatus::Cancelled
    );
    assert!(!dispatch.claim_is_current(&claim, 104).unwrap());
    assert!(dispatch.list_locks().unwrap().is_empty());
    assert!(dispatch
        .list_events(&EventFilter {
            event_type: Some("runtime.queue_item.available".to_owned()),
            ..EventFilter::default()
        })
        .unwrap()
        .iter()
        .all(|event| event.idempotency_key.as_deref()
            != Some("queue_item:evt_cancel_running:worker:available:2")));
}

#[test]
fn recovery_finalizes_requested_running_work_before_claiming() {
    let mut dispatch = Dispatch::open_in_memory().unwrap();
    let mut event = journal_event("evt_cancel_recovery", Some("ingress:cancel-recovery"));
    event.event_type = "work.requested".to_owned();
    route_available_work(
        &mut dispatch,
        event,
        "worker",
        SessionRule::PerEvent,
        vec!["repo:zeta".to_owned()],
    );
    let claim = dispatch
        .claim_next_queue_item(
            "worker-a",
            ClaimToken::new("cancel-recovery-token").unwrap(),
            1_000,
            100,
        )
        .unwrap()
        .unwrap();
    dispatch
        .start_claimed_attempt(
            &claim,
            101,
            RuntimeEventIdentity::new("recovery-claimed", 101).unwrap(),
            RuntimeEventIdentity::new("recovery-started", 102).unwrap(),
            "2026-08-12T10:00:00Z",
            None,
        )
        .unwrap();
    let cancellation = dispatch
        .cancel_queue_item(
            claim.queue_item_id(),
            None,
            Some("worker stopped"),
            CancellationIdentities::new(
                RuntimeEventIdentity::new("recovery-cancel-request", 103).unwrap(),
                RuntimeEventIdentity::new("unused-recovery-cancel", 103).unwrap(),
            ),
        )
        .unwrap();
    assert_eq!(cancellation.status(), CancellationStatus::Cancelling);

    dispatch.rebuild_projections().unwrap();

    let recovered = dispatch.queue_item(claim.queue_item_id()).unwrap().unwrap();
    assert_eq!(recovered.status(), QueueItemStatus::Available);
    assert_eq!(
        recovered.cancellation_requested_event_id(),
        Some("recovery-cancel-request")
    );
    assert!(dispatch
        .claim_next_queue_item(
            "worker-b",
            ClaimToken::new("blocked-after-recovery").unwrap(),
            1_000,
            200,
        )
        .unwrap()
        .is_none());

    let finalized = dispatch
        .finalize_next_requested_cancellation(
            CancellationFinalizationIdentities::new(
                RuntimeEventIdentity::new("recovered-attempt-cancelled", 201).unwrap(),
                RuntimeEventIdentity::new("recovered-queue-cancelled", 201).unwrap(),
            ),
            "1970-01-01T00:00:00.201Z",
        )
        .unwrap()
        .unwrap();

    assert_eq!(finalized.queue_item_id(), Some(claim.queue_item_id()));
    assert_eq!(finalized.events().len(), 2);
    assert_eq!(
        finalized.events()[0].event_type,
        "runtime.attempt.cancelled"
    );
    assert_eq!(
        finalized.events()[1].event_type,
        "runtime.queue_item.cancelled"
    );
    assert!(finalized
        .events()
        .iter()
        .all(|event| event.caused_by.as_deref() == Some("recovery-cancel-request")));
    assert_eq!(
        finalized.events()[0].payload["result"]["reason"],
        "worker stopped"
    );
    assert_eq!(
        dispatch.list_attempts().unwrap()[0].status(),
        AttemptStatus::Cancelled
    );
    assert_eq!(
        dispatch
            .queue_item(claim.queue_item_id())
            .unwrap()
            .unwrap()
            .status(),
        QueueItemStatus::Cancelled
    );
    assert!(dispatch
        .finalize_next_requested_cancellation(
            CancellationFinalizationIdentities::new(
                RuntimeEventIdentity::new("unused-recovered-attempt", 202).unwrap(),
                RuntimeEventIdentity::new("unused-recovered-queue", 202).unwrap(),
            ),
            "1970-01-01T00:00:00.202Z",
        )
        .unwrap()
        .is_none());
    dispatch.rebuild_projections().unwrap();
    assert_eq!(
        dispatch.list_attempts().unwrap()[0].status(),
        AttemptStatus::Cancelled
    );
}

#[test]
fn completion_vector_commits_ordered_controls_atomically() {
    let case = scripted_case("completion", "ordered_atomic_success");
    let event = runtime_event(&case["triggering_event"]);
    let mut dispatch = Dispatch::open_in_memory().unwrap();
    let queue_item_id = route_specific_work(
        &mut dispatch,
        event,
        case["agent_id"].as_str().unwrap(),
        "agent/worker/evt_complete",
        case["project_revision"].as_str(),
    );
    let claim = dispatch
        .claim_next_queue_item(
            "local",
            ClaimToken::new("completion-token").unwrap(),
            1_000,
            200,
        )
        .unwrap()
        .unwrap();
    assert_eq!(claim.queue_item_id(), &queue_item_id);
    let started = dispatch
        .start_claimed_attempt(
            &claim,
            201,
            RuntimeEventIdentity::new("complete-claimed", 201).unwrap(),
            RuntimeEventIdentity::new("complete-started", 202).unwrap(),
            "2026-08-12T10:00:00Z",
            None,
        )
        .unwrap();
    let completion_identities: Vec<RuntimeEventIdentity> = (0..5)
        .map(|position| {
            RuntimeEventIdentity::new(format!("complete-terminal-{position}"), 210 + position)
                .unwrap()
        })
        .collect();

    let mut metadata = case["result"].as_object().unwrap().clone();
    metadata.remove("publish_event_requests");
    metadata.remove("wait_requests");
    let controls = vec![
        AttemptControl::publish(
            "pub-first",
            "work.first",
            serde_json::from_value(json!({"position": 0})).unwrap(),
            None,
            0,
        ),
        AttemptControl::wait(
            "wait-middle",
            "work.ready",
            serde_json::from_value(json!({"work_id": "42"})).unwrap(),
            Some("2030-01-02T03:04:05+00:00".to_owned()),
            1,
        ),
        AttemptControl::publish(
            "pub-future",
            "work.future",
            serde_json::from_value(json!({"position": 2})).unwrap(),
            Some("2999-01-01T00:00:00Z".to_owned()),
            2,
        ),
    ];
    let terminal = dispatch
        .complete_claimed_attempt(
            &claim,
            203,
            &completion_identities,
            &AttemptCompletion::new(
                "2026-08-12T10:00:01Z",
                AttemptCompletionDisposition::Succeeded,
                metadata,
                controls,
            ),
        )
        .unwrap();

    let mut observed = started.events().to_vec();
    observed.extend(terminal);
    let expected = case["expected"]["events"].as_array().unwrap();
    assert_eq!(observed.len(), expected.len());
    for (actual, expected) in observed.iter().zip(expected) {
        assert_eq!(actual.event_type, expected["type"]);
        assert_eq!(
            actual.idempotency_key.as_deref(),
            expected["idempotency_key"].as_str()
        );
        let expected_parent = match expected["caused_by"].as_str() {
            Some("$attempt_completed") => Some("complete-terminal-0"),
            parent => parent,
        };
        assert_eq!(actual.caused_by.as_deref(), expected_parent);
        for (key, value) in expected["payload"].as_object().unwrap() {
            assert_eq!(actual.payload.get(key), Some(value), "payload field {key}");
        }
    }
    assert_eq!(
        dispatch
            .queue_item(&queue_item_id)
            .unwrap()
            .unwrap()
            .status(),
        QueueItemStatus::Completed
    );
    assert_eq!(
        dispatch.list_attempts().unwrap()[0].status(),
        AttemptStatus::Completed
    );
    assert!(!dispatch.claim_is_current(&claim, 203).unwrap());
    let journal_types: Vec<String> = dispatch
        .list_events(&EventFilter::default())
        .unwrap()
        .into_iter()
        .map(|event| event.event_type)
        .collect();
    assert_eq!(
        &journal_types[journal_types.len() - 5..],
        [
            "runtime.attempt.completed",
            "work.first",
            "runtime.wait.created",
            "runtime.deferred_publication.created",
            "runtime.queue_item.completed",
        ]
    );
}

#[test]
fn unsorted_typed_controls_use_global_position_for_execution_and_durable_arrays() {
    let (mut dispatch, claim, _queue_item_id) = running_completion_attempt("unsorted-controls");
    let controls = vec![
        AttemptControl::publish(
            "pub-third",
            "work.third",
            serde_json::from_value(json!({"position": 3})).unwrap(),
            None,
            3,
        ),
        AttemptControl::publish(
            "pub-second",
            "work.second",
            serde_json::from_value(json!({"position": 2})).unwrap(),
            None,
            2,
        ),
        AttemptControl::publish(
            "pub-first",
            "work.first",
            serde_json::from_value(json!({"position": 1})).unwrap(),
            None,
            1,
        ),
        AttemptControl::wait(
            "wait-zero",
            "work.zero",
            serde_json::from_value(json!({"position": 0})).unwrap(),
            None,
            0,
        ),
    ];

    let events = dispatch
        .complete_claimed_attempt(
            &claim,
            203,
            &[
                RuntimeEventIdentity::new("unsorted-attempt", 203).unwrap(),
                RuntimeEventIdentity::new("unsorted-zero", 204).unwrap(),
                RuntimeEventIdentity::new("unsorted-first", 205).unwrap(),
                RuntimeEventIdentity::new("unsorted-second", 206).unwrap(),
                RuntimeEventIdentity::new("unsorted-third", 207).unwrap(),
                RuntimeEventIdentity::new("unsorted-queue", 208).unwrap(),
            ],
            &AttemptCompletion::new(
                "2026-08-12T10:00:01Z",
                AttemptCompletionDisposition::Succeeded,
                Map::new(),
                controls,
            ),
        )
        .unwrap();

    let mut event_types = Vec::new();
    for event in &events {
        event_types.push(event.event_type.as_str());
    }
    assert_eq!(
        event_types,
        [
            "runtime.attempt.completed",
            "runtime.wait.created",
            "work.first",
            "work.second",
            "work.third",
            "runtime.queue_item.completed",
        ]
    );
    assert_eq!(
        events[0].payload["result"]["publish_event_requests"],
        json!([
            {
                "handle": "pub-first",
                "event_type": "work.first",
                "payload": {"position": 1},
                "at": null,
                "position": 1,
            },
            {
                "handle": "pub-second",
                "event_type": "work.second",
                "payload": {"position": 2},
                "at": null,
                "position": 2,
            },
            {
                "handle": "pub-third",
                "event_type": "work.third",
                "payload": {"position": 3},
                "at": null,
                "position": 3,
            },
        ])
    );
    assert_eq!(
        events[0].payload["result"]["wait_requests"],
        json!([
            {
                "handle": "wait-zero",
                "event_type": "work.zero",
                "fields": {"position": 0},
                "deadline": null,
                "position": 0,
            },
        ])
    );
    assert_eq!(events[5].payload["result"], events[0].payload["result"]);
}

#[test]
fn completion_cancels_owned_resources_in_control_order() {
    let mut dispatch = Dispatch::open_in_memory().unwrap();
    let event = Event {
        id: "evt-cancel-control".to_owned(),
        event_type: "work.requested".to_owned(),
        source: "test".to_owned(),
        payload: Map::new(),
        idempotency_key: Some("ingress:cancel-control".to_owned()),
        caused_by: None,
        session_id: None,
        run_id: None,
        turn_id: None,
        timestamp_ms: 100,
        cursor: None,
    };
    let session_id = "agent/worker/evt-cancel-control";
    route_specific_work(&mut dispatch, event, "worker", session_id, None);
    dispatch
        .append_trusted_event(Event {
            id: "wait-control-created".to_owned(),
            event_type: "runtime.wait.created".to_owned(),
            source: "zeta".to_owned(),
            payload: serde_json::from_value(json!({
                "handle": "wait_control",
                "agent_id": "worker",
                "session_id": session_id,
                "event_type": "work.obsolete",
                "fields": {},
                "deadline": null,
                "source_queue_item_id": "qi-prior-worker",
            }))
            .unwrap(),
            idempotency_key: Some("agent.wait:qi-prior-worker:0".to_owned()),
            caused_by: Some("prior-attempt-completed".to_owned()),
            session_id: Some(session_id.to_owned()),
            run_id: Some("run-prior".to_owned()),
            turn_id: None,
            timestamp_ms: 150,
            cursor: None,
        })
        .unwrap();
    let claim = dispatch
        .claim_next_queue_item(
            "local",
            ClaimToken::new("cancel-control-token").unwrap(),
            1_000,
            200,
        )
        .unwrap()
        .unwrap();
    dispatch
        .start_claimed_attempt(
            &claim,
            201,
            RuntimeEventIdentity::new("cancel-control-claimed", 201).unwrap(),
            RuntimeEventIdentity::new("cancel-control-started", 202).unwrap(),
            "2026-08-12T10:00:00Z",
            None,
        )
        .unwrap();

    let events = dispatch
        .complete_claimed_attempt(
            &claim,
            203,
            &[
                RuntimeEventIdentity::new("cancel-control-completed", 203).unwrap(),
                RuntimeEventIdentity::new("cancel-control-resource", 204).unwrap(),
                RuntimeEventIdentity::new("cancel-control-queue", 205).unwrap(),
            ],
            &AttemptCompletion::new(
                "2026-08-12T10:00:01Z",
                AttemptCompletionDisposition::Succeeded,
                serde_json::from_value(json!({
                    "final_answer": "done",
                }))
                .unwrap(),
                vec![AttemptControl::cancel(
                    "wait_control",
                    Some("superseded".to_owned()),
                    "worker",
                    session_id,
                    0,
                )],
            ),
        )
        .unwrap();

    let event_types: Vec<_> = events
        .iter()
        .map(|event| event.event_type.as_str())
        .collect();
    assert_eq!(
        event_types,
        [
            "runtime.attempt.completed",
            "runtime.wait.cancelled",
            "runtime.queue_item.completed",
        ]
    );
    assert_eq!(events[1].payload["reason"], "superseded");
    assert_eq!(events[1].payload["cancelled_by_agent_id"], "worker");
    assert_eq!(events[1].payload["cancelled_by_session_id"], session_id);
    assert_eq!(
        dispatch.list_waits().unwrap()[0].status(),
        WaitStatus::Cancelled
    );
}

#[test]
fn invalid_completion_proposal_writes_no_partial_success() {
    let case = scripted_case("completion", "invalid_proposal_is_atomic_failure");
    let event = runtime_event(&case["triggering_event"]);
    let mut dispatch = Dispatch::open_in_memory().unwrap();
    let queue_item_id = route_specific_work(
        &mut dispatch,
        event,
        case["agent_id"].as_str().unwrap(),
        "agent/worker/evt_complete",
        None,
    );
    let claim = dispatch
        .claim_next_queue_item(
            "local",
            ClaimToken::new("invalid-completion-token").unwrap(),
            1_000,
            200,
        )
        .unwrap()
        .unwrap();
    dispatch
        .start_claimed_attempt(
            &claim,
            201,
            RuntimeEventIdentity::new("invalid-claimed", 201).unwrap(),
            RuntimeEventIdentity::new("invalid-started", 202).unwrap(),
            "2026-08-12T10:00:00Z",
            None,
        )
        .unwrap();
    let before = dispatch.list_events(&EventFilter::default()).unwrap().len();

    let error = dispatch
        .complete_claimed_attempt(
            &claim,
            203,
            &[
                RuntimeEventIdentity::new("invalid-completed", 203).unwrap(),
                RuntimeEventIdentity::new("invalid-cancel-resource", 204).unwrap(),
                RuntimeEventIdentity::new("invalid-queue-completed", 205).unwrap(),
            ],
            &AttemptCompletion::new(
                "2026-08-12T10:00:01Z",
                AttemptCompletionDisposition::Succeeded,
                serde_json::from_value(json!({"final_answer": "invalid"})).unwrap(),
                vec![AttemptControl::cancel(
                    "wait_999999999999999999999999",
                    Some("missing".to_owned()),
                    "worker",
                    "agent/worker/evt_complete",
                    0,
                )],
            ),
        )
        .unwrap_err();

    assert_eq!(error.reason(), "cancellation_resource_not_found");
    assert_eq!(
        dispatch.list_events(&EventFilter::default()).unwrap().len(),
        before
    );
    assert_eq!(
        dispatch.list_attempts().unwrap()[0].status(),
        AttemptStatus::Running
    );
    assert_eq!(
        dispatch
            .queue_item(&queue_item_id)
            .unwrap()
            .unwrap()
            .status(),
        QueueItemStatus::Claimed
    );
    assert!(dispatch.claim_is_current(&claim, 203).unwrap());

    let failed = dispatch
        .fail_claimed_attempt(
            &claim,
            204,
            [
                RuntimeEventIdentity::new("invalid-attempt-failed", 204).unwrap(),
                RuntimeEventIdentity::new("invalid-dead-lettered", 205).unwrap(),
            ],
            &AttemptFailure::new(
                "2026-08-12T10:00:02Z",
                "unknown cancellation handle",
                AttemptFailureCode::MalformedEventPayload,
                RetryPolicy::default(),
            ),
        )
        .unwrap();
    assert_eq!(failed[0].event_type, "runtime.attempt.failed");
    assert_eq!(failed[1].event_type, "runtime.queue_item.dead_lettered");
}

#[test]
fn explicit_cancelled_completion_records_proposals_without_applying_them() {
    let (mut dispatch, claim, queue_item_id) = running_completion_attempt("typed-cancelled");
    let controls = vec![AttemptControl::publish(
        "pub-cancelled",
        "work.must-not-publish",
        Map::new(),
        None,
        0,
    )];

    let events = dispatch
        .complete_claimed_attempt(
            &claim,
            203,
            &[
                RuntimeEventIdentity::new("typed-cancelled-attempt", 203).unwrap(),
                RuntimeEventIdentity::new("typed-cancelled-unused", 204).unwrap(),
                RuntimeEventIdentity::new("typed-cancelled-queue", 205).unwrap(),
            ],
            &AttemptCompletion::new(
                "2026-08-12T10:00:01Z",
                AttemptCompletionDisposition::Cancelled,
                serde_json::from_value(json!({"final_answer": "partial"})).unwrap(),
                controls,
            ),
        )
        .unwrap();

    assert_eq!(events.len(), 2);
    assert_eq!(events[0].id, "typed-cancelled-attempt");
    assert_eq!(events[1].id, "typed-cancelled-queue");
    assert_eq!(events[0].event_type, "runtime.attempt.cancelled");
    assert_eq!(events[1].event_type, "runtime.queue_item.cancelled");
    assert_eq!(events[0].payload["result"]["outcome"], "cancelled");
    assert_eq!(events[0].payload["result"]["stop_reason"], "aborted");
    assert_eq!(
        events[0].payload["result"]["publish_event_requests"][0]["handle"],
        "pub-cancelled"
    );
    assert_eq!(
        dispatch
            .list_events(&EventFilter::default())
            .unwrap()
            .iter()
            .filter(|event| event.event_type == "work.must-not-publish")
            .count(),
        0
    );
    assert_eq!(
        dispatch
            .queue_item(&queue_item_id)
            .unwrap()
            .unwrap()
            .status(),
        QueueItemStatus::Cancelled
    );
}

#[test]
fn cancelled_completion_rejects_excess_identities_atomically() {
    let (mut dispatch, claim, _queue_item_id) =
        running_completion_attempt("cancelled-excess-identities");
    let before = dispatch.list_events(&EventFilter::default()).unwrap().len();

    let error = dispatch
        .complete_claimed_attempt(
            &claim,
            203,
            &[
                RuntimeEventIdentity::new("cancelled-excess-attempt", 203).unwrap(),
                RuntimeEventIdentity::new("cancelled-excess-control", 204).unwrap(),
                RuntimeEventIdentity::new("cancelled-excess-arbitrary", 205).unwrap(),
                RuntimeEventIdentity::new("cancelled-excess-queue", 206).unwrap(),
            ],
            &AttemptCompletion::new(
                "2026-08-12T10:00:01Z",
                AttemptCompletionDisposition::Cancelled,
                Map::new(),
                vec![AttemptControl::publish(
                    "pub-cancelled-excess",
                    "work.must-not-publish",
                    Map::new(),
                    None,
                    0,
                )],
            ),
        )
        .unwrap_err();

    match error {
        DispatchError::RuntimeEventIdentityCount { expected, actual } => {
            assert_eq!(expected, 3);
            assert_eq!(actual, 4);
        }
        error => panic!("unexpected error: {error}"),
    }
    assert_eq!(
        dispatch.list_events(&EventFilter::default()).unwrap().len(),
        before
    );
    assert_eq!(
        dispatch.list_attempts().unwrap()[0].status(),
        AttemptStatus::Running
    );
    assert!(dispatch.claim_is_current(&claim, 203).unwrap());
}

#[test]
fn cancelled_disposition_still_validates_typed_controls() {
    let (mut dispatch, claim, _queue_item_id) = running_completion_attempt("cancel-validation");
    let before = dispatch.list_events(&EventFilter::default()).unwrap().len();

    let error = dispatch
        .complete_claimed_attempt(
            &claim,
            203,
            &[
                RuntimeEventIdentity::new("cancel-validation-attempt", 203).unwrap(),
                RuntimeEventIdentity::new("cancel-validation-first", 204).unwrap(),
                RuntimeEventIdentity::new("cancel-validation-second", 205).unwrap(),
                RuntimeEventIdentity::new("cancel-validation-queue", 206).unwrap(),
            ],
            &AttemptCompletion::new(
                "2026-08-12T10:00:01Z",
                AttemptCompletionDisposition::Cancelled,
                Map::new(),
                vec![
                    AttemptControl::publish("pub-first", "work.first", Map::new(), None, 0),
                    AttemptControl::wait("wait-second", "work.second", Map::new(), None, 0),
                ],
            ),
        )
        .unwrap_err();

    match error {
        DispatchError::InvalidCompletion { field } => assert_eq!(field, "control.position"),
        error => panic!("unexpected error: {error}"),
    }
    assert_eq!(
        dispatch.list_events(&EventFilter::default()).unwrap().len(),
        before
    );
    assert!(dispatch.claim_is_current(&claim, 203).unwrap());
}

#[test]
fn durable_cancellation_wins_over_a_valid_success_proposal() {
    let (mut dispatch, claim, queue_item_id) = running_completion_attempt("late-cancel");
    let cancellation = dispatch
        .cancel_queue_item(
            &queue_item_id,
            Some("agent/worker/evt-late-cancel"),
            Some("changed direction"),
            CancellationIdentities::new(
                RuntimeEventIdentity::new("late-cancel-requested", 203).unwrap(),
                RuntimeEventIdentity::new("late-cancel-unused", 204).unwrap(),
            ),
        )
        .unwrap();
    assert_eq!(cancellation.status(), CancellationStatus::Cancelling);

    let events = dispatch
        .complete_claimed_attempt(
            &claim,
            204,
            &[
                RuntimeEventIdentity::new("late-cancel-attempt", 204).unwrap(),
                RuntimeEventIdentity::new("late-cancel-control-unused", 205).unwrap(),
                RuntimeEventIdentity::new("late-cancel-queue", 206).unwrap(),
            ],
            &AttemptCompletion::new(
                "2026-08-12T10:00:01Z",
                AttemptCompletionDisposition::Succeeded,
                Map::new(),
                vec![AttemptControl::publish(
                    "pub-late-cancel",
                    "work.must-not-publish",
                    Map::new(),
                    None,
                    0,
                )],
            ),
        )
        .unwrap();

    assert_eq!(events.len(), 2);
    assert_eq!(events[0].event_type, "runtime.attempt.cancelled");
    assert_eq!(events[1].event_type, "runtime.queue_item.cancelled");
    assert_eq!(
        events[0].payload["result"]["publish_event_requests"][0]["handle"],
        "pub-late-cancel"
    );
    assert_eq!(
        dispatch
            .list_events(&EventFilter::default())
            .unwrap()
            .iter()
            .filter(|event| event.event_type == "work.must-not-publish")
            .count(),
        0
    );
}

#[test]
fn successful_disposition_does_not_infer_cancellation_from_metadata() {
    let (mut dispatch, claim, queue_item_id) = running_completion_attempt("typed-succeeded");

    let events = dispatch
        .complete_claimed_attempt(
            &claim,
            203,
            &[
                RuntimeEventIdentity::new("typed-succeeded-attempt", 203).unwrap(),
                RuntimeEventIdentity::new("typed-succeeded-queue", 204).unwrap(),
            ],
            &AttemptCompletion::new(
                "2026-08-12T10:00:01Z",
                AttemptCompletionDisposition::Succeeded,
                serde_json::from_value(json!({"outcome": "cancelled"})).unwrap(),
                Vec::new(),
            ),
        )
        .unwrap();

    assert_eq!(events[0].event_type, "runtime.attempt.completed");
    assert_eq!(events[1].event_type, "runtime.queue_item.completed");
    assert_eq!(
        dispatch
            .queue_item(&queue_item_id)
            .unwrap()
            .unwrap()
            .status(),
        QueueItemStatus::Completed
    );
}

#[test]
fn reserved_control_metadata_is_rejected_before_any_success_fact() {
    let (mut dispatch, claim, _queue_item_id) = running_completion_attempt("typed-reserved");
    let before = dispatch.list_events(&EventFilter::default()).unwrap().len();

    let error = dispatch
        .complete_claimed_attempt(
            &claim,
            203,
            &[
                RuntimeEventIdentity::new("typed-reserved-attempt", 203).unwrap(),
                RuntimeEventIdentity::new("typed-reserved-queue", 204).unwrap(),
            ],
            &AttemptCompletion::new(
                "2026-08-12T10:00:01Z",
                AttemptCompletionDisposition::Succeeded,
                serde_json::from_value(json!({"publish_event_requests": []})).unwrap(),
                Vec::new(),
            ),
        )
        .unwrap_err();

    match error {
        DispatchError::InvalidCompletion { field } => {
            assert_eq!(field, "publish_event_requests")
        }
        error => panic!("unexpected error: {error}"),
    }
    assert_eq!(
        dispatch.list_events(&EventFilter::default()).unwrap().len(),
        before
    );
    assert!(dispatch.claim_is_current(&claim, 203).unwrap());
}

#[test]
fn duplicate_typed_control_positions_are_rejected_atomically() {
    let (mut dispatch, claim, _queue_item_id) = running_completion_attempt("typed-duplicate");
    let before = dispatch.list_events(&EventFilter::default()).unwrap().len();
    let controls = vec![
        AttemptControl::publish("pub-first", "work.first", Map::new(), None, 0),
        AttemptControl::wait("wait-second", "work.second", Map::new(), None, 0),
    ];

    let error = dispatch
        .complete_claimed_attempt(
            &claim,
            203,
            &[
                RuntimeEventIdentity::new("typed-duplicate-attempt", 203).unwrap(),
                RuntimeEventIdentity::new("typed-duplicate-first", 204).unwrap(),
                RuntimeEventIdentity::new("typed-duplicate-second", 205).unwrap(),
                RuntimeEventIdentity::new("typed-duplicate-queue", 206).unwrap(),
            ],
            &AttemptCompletion::new(
                "2026-08-12T10:00:01Z",
                AttemptCompletionDisposition::Succeeded,
                Map::new(),
                controls,
            ),
        )
        .unwrap_err();

    match error {
        DispatchError::InvalidCompletion { field } => assert_eq!(field, "control.position"),
        error => panic!("unexpected error: {error}"),
    }
    assert_eq!(
        dispatch.list_events(&EventFilter::default()).unwrap().len(),
        before
    );
    assert!(dispatch.claim_is_current(&claim, 203).unwrap());
}

#[test]
fn invalid_typed_control_fields_are_rejected_before_any_success_fact() {
    let cases = [
        (
            "typed-empty-event",
            AttemptControl::publish("pub-invalid", "", Map::new(), None, 0),
            "publish_event_requests.event_type",
        ),
        (
            "typed-deadline",
            AttemptControl::wait(
                "wait-invalid",
                "work.ready",
                Map::new(),
                Some("tomorrow".to_owned()),
                0,
            ),
            "wait_requests.deadline",
        ),
        (
            "typed-reason",
            AttemptControl::cancel("wait-invalid", Some(String::new()), "worker", "session", 0),
            "cancel_requests.reason",
        ),
    ];

    for (prefix, control, field) in cases {
        let (mut dispatch, claim, _queue_item_id) = running_completion_attempt(prefix);
        let before = dispatch.list_events(&EventFilter::default()).unwrap().len();
        let error = dispatch
            .complete_claimed_attempt(
                &claim,
                203,
                &[
                    RuntimeEventIdentity::new(format!("{prefix}-attempt"), 203).unwrap(),
                    RuntimeEventIdentity::new(format!("{prefix}-control"), 204).unwrap(),
                    RuntimeEventIdentity::new(format!("{prefix}-queue"), 205).unwrap(),
                ],
                &AttemptCompletion::new(
                    "2026-08-12T10:00:01Z",
                    AttemptCompletionDisposition::Succeeded,
                    Map::new(),
                    vec![control],
                ),
            )
            .unwrap_err();
        match error {
            DispatchError::InvalidCompletion { field: actual } => assert_eq!(actual, field),
            error => panic!("unexpected error: {error}"),
        }
        assert_eq!(
            dispatch.list_events(&EventFilter::default()).unwrap().len(),
            before
        );
        assert!(dispatch.claim_is_current(&claim, 203).unwrap());
    }
}

#[test]
fn matching_event_resumes_wait_once() {
    let case = scripted_case("waits", "matching_event_resumes_once");
    let mut dispatch = Dispatch::open_in_memory().unwrap();
    let wait_created = runtime_event(&case["created_event"]);
    dispatch.append_trusted_event(wait_created.clone()).unwrap();

    let matching_draft = &case["matching_draft"];
    let matching_input = Event {
        id: "wait-matching-input".to_owned(),
        event_type: matching_draft["event_type"].as_str().unwrap().to_owned(),
        source: matching_draft["source"].as_str().unwrap().to_owned(),
        payload: matching_draft["payload"].as_object().unwrap().clone(),
        idempotency_key: matching_draft["idempotency_key"]
            .as_str()
            .map(str::to_owned),
        caused_by: None,
        session_id: None,
        run_id: None,
        turn_id: None,
        timestamp_ms: 600,
        cursor: None,
    };
    let first = dispatch.ingest_event(matching_input.clone()).unwrap();
    let retry = dispatch.ingest_event(matching_input.clone()).unwrap();
    assert_eq!([first.inserted, retry.inserted], [true, false]);

    let resumed = dispatch
        .resume_waits_for_event(
            &matching_input.id,
            &[
                RuntimeEventIdentity::new("wait-matched-1", 601).unwrap(),
                RuntimeEventIdentity::new("wait-continuation-1", 602).unwrap(),
            ],
        )
        .unwrap();
    assert_eq!(resumed.len(), 2);
    assert!(dispatch
        .resume_waits_for_event(&matching_input.id, &[])
        .unwrap()
        .is_empty());

    let events = dispatch.list_events(&EventFilter::default()).unwrap();
    let expected = case["expected"]["events"].as_array().unwrap();
    assert_eq!(events.len(), expected.len());
    for (actual, expected) in events.iter().zip(expected) {
        assert_eq!(actual.event_type, expected["type"]);
        let expected_idempotency_key = match expected["idempotency_key"].as_str() {
            Some("queue_item:$wait_matched:issue-agent:available") => {
                Some("queue_item:wait-matched-1:issue-agent:available")
            }
            key => key,
        };
        assert_eq!(actual.idempotency_key.as_deref(), expected_idempotency_key);
    }
    assert_eq!(
        events[2].caused_by.as_deref(),
        Some(matching_input.id.as_str())
    );
    assert_eq!(events[2].payload["matched_event_id"], matching_input.id);
    assert_eq!(
        events[2].payload["payload"],
        Value::Object(matching_input.payload.clone())
    );
    assert_eq!(events[3].caused_by.as_deref(), Some("wait-matched-1"));
    assert_eq!(
        events[3].payload["queue_item_id"],
        "qi_wait-matched-1_issue-agent"
    );

    let waits = dispatch.list_waits().unwrap();
    assert_eq!(waits.len(), 1);
    assert_eq!(waits[0].status(), WaitStatus::Matched);
    assert_eq!(
        waits[0].matched_event_id(),
        Some(matching_input.id.as_str())
    );
    let mut statuses: Vec<_> = dispatch
        .list_queue_items()
        .unwrap()
        .into_iter()
        .map(|item| item.status())
        .collect();
    statuses.sort_by_key(|status| status.to_string());
    assert_eq!(
        statuses,
        [QueueItemStatus::Available, QueueItemStatus::Pending]
    );
}

#[test]
fn due_wait_timeout_resumes_session_once() {
    let mut dispatch = Dispatch::open_in_memory().unwrap();
    dispatch
        .append_trusted_event(Event {
            id: "wait-timeout-created".to_owned(),
            event_type: "runtime.wait.created".to_owned(),
            source: "zeta".to_owned(),
            payload: serde_json::from_value(json!({
                "handle": "wait_timeout",
                "agent_id": "issue-agent",
                "session_id": "session-timeout",
                "event_type": "github.issue.updated",
                "fields": {},
                "deadline": "1970-01-01T00:00:01.250000+00:00",
                "source_queue_item_id": "qi-timeout-source",
                "project_revision": "revision-1",
            }))
            .unwrap(),
            idempotency_key: Some("agent.wait:qi-timeout-source:0".to_owned()),
            caused_by: Some("attempt-timeout-completed".to_owned()),
            session_id: Some("session-timeout".to_owned()),
            run_id: Some("run-timeout".to_owned()),
            turn_id: None,
            timestamp_ms: 500,
            cursor: None,
        })
        .unwrap();

    assert!(dispatch
        .timeout_next_due_wait(
            1_249,
            [
                RuntimeEventIdentity::new("wait-timeout-too-early", 1_249).unwrap(),
                RuntimeEventIdentity::new("wait-timeout-continuation-too-early", 1_249).unwrap(),
            ],
        )
        .unwrap()
        .is_none());
    let timed_out = dispatch
        .timeout_next_due_wait(
            1_250,
            [
                RuntimeEventIdentity::new("wait-timeout-terminal", 1_250).unwrap(),
                RuntimeEventIdentity::new("wait-timeout-continuation", 1_251).unwrap(),
            ],
        )
        .unwrap()
        .unwrap();
    assert_eq!(timed_out.len(), 2);
    assert!(dispatch
        .timeout_next_due_wait(
            1_250,
            [
                RuntimeEventIdentity::new("wait-timeout-retry", 1_252).unwrap(),
                RuntimeEventIdentity::new("wait-timeout-continuation-retry", 1_253).unwrap(),
            ],
        )
        .unwrap()
        .is_none());

    assert_eq!(timed_out[0].event_type, "runtime.wait.timed_out");
    assert_eq!(
        timed_out[0].idempotency_key.as_deref(),
        Some("wait.timed_out:wait_timeout")
    );
    assert_eq!(
        timed_out[0].caused_by.as_deref(),
        Some("wait-timeout-created")
    );
    assert_eq!(
        Value::Object(timed_out[0].payload.clone()),
        json!({
            "handle": "wait_timeout",
            "agent_id": "issue-agent",
            "session_id": "session-timeout",
            "deadline": "1970-01-01T00:00:01.250000+00:00",
            "project_revision": "revision-1",
        })
    );
    assert_eq!(timed_out[1].event_type, "runtime.queue_item.available");
    assert_eq!(
        timed_out[1].caused_by.as_deref(),
        Some("wait-timeout-terminal")
    );
    assert_eq!(
        timed_out[1].payload["queue_item_id"],
        "qi_wait-timeout-terminal_issue-agent"
    );

    let waits = dispatch.list_waits().unwrap();
    assert_eq!(waits[0].status(), WaitStatus::TimedOut);
    assert_eq!(waits[0].terminal_event_id(), Some("wait-timeout-terminal"));
    let queue = dispatch
        .list_queue_items()
        .unwrap()
        .into_iter()
        .find(|item| item.id().as_str() == "qi_wait-timeout-terminal_issue-agent")
        .unwrap();
    assert_eq!(queue.status(), QueueItemStatus::Available);
}

#[test]
fn resource_cancellation_is_authorized_atomic_and_idempotent() {
    let mut dispatch = Dispatch::open_in_memory().unwrap();
    dispatch
        .append_trusted_event(Event {
            id: "wait-cancel-created".to_owned(),
            event_type: "runtime.wait.created".to_owned(),
            source: "zeta".to_owned(),
            payload: serde_json::from_value(json!({
                "handle": "wait_cancel",
                "agent_id": "issue-agent",
                "session_id": "session-cancel",
                "event_type": "github.issue.updated",
                "fields": {},
                "deadline": null,
                "source_queue_item_id": "qi-cancel-source",
                "project_revision": "revision-1",
            }))
            .unwrap(),
            idempotency_key: Some("agent.wait:qi-cancel-source:0".to_owned()),
            caused_by: Some("attempt-cancel-completed".to_owned()),
            session_id: Some("session-cancel".to_owned()),
            run_id: Some("run-cancel".to_owned()),
            turn_id: None,
            timestamp_ms: 500,
            cursor: None,
        })
        .unwrap();

    let unauthorized = dispatch
        .cancel_resource(
            "wait_cancel",
            Some("no longer needed"),
            Some("another-agent"),
            Some("session-cancel"),
            RuntimeEventIdentity::new("wait-cancel-unauthorized", 600).unwrap(),
        )
        .unwrap_err();
    assert_eq!(unauthorized.reason(), "cancellation_authority_mismatch");
    assert_eq!(
        dispatch.list_waits().unwrap()[0].status(),
        WaitStatus::Active
    );

    let cancelled = dispatch
        .cancel_resource(
            "wait_cancel",
            Some("no longer needed"),
            Some("issue-agent"),
            Some("session-cancel"),
            RuntimeEventIdentity::new("wait-cancel-terminal", 601).unwrap(),
        )
        .unwrap();
    assert_eq!(cancelled.resource_kind(), ResourceKind::Wait);
    assert_eq!(cancelled.status(), ResourceCancellationStatus::Cancelled);
    assert!(cancelled.changed());
    let event = cancelled.event().unwrap();
    assert_eq!(event.event_type, "runtime.wait.cancelled");
    assert_eq!(
        Value::Object(event.payload.clone()),
        json!({
            "handle": "wait_cancel",
            "agent_id": "issue-agent",
            "session_id": "session-cancel",
            "reason": "no longer needed",
            "cancelled_by_agent_id": "issue-agent",
            "cancelled_by_session_id": "session-cancel",
        })
    );
    assert_eq!(event.caused_by.as_deref(), Some("wait-cancel-created"));

    let repeated = dispatch
        .cancel_resource(
            "wait_cancel",
            Some("different retry reason"),
            Some("issue-agent"),
            Some("session-cancel"),
            RuntimeEventIdentity::new("wait-cancel-retry", 602).unwrap(),
        )
        .unwrap();
    assert_eq!(repeated.status(), ResourceCancellationStatus::Cancelled);
    assert!(!repeated.changed());
    assert!(repeated.event().is_none());

    dispatch
        .append_trusted_event(Event {
            id: "deferred-cancel-created".to_owned(),
            event_type: "runtime.deferred_publication.created".to_owned(),
            source: "zeta".to_owned(),
            payload: serde_json::from_value(json!({
                "handle": "pub_cancel",
                "event_type": "report.ready",
                "payload": {"report_id": "cancelled"},
                "publish_at": "1970-01-01T00:00:01+00:00",
                "source_agent_id": "reporter",
                "source_session_id": "session-report",
                "source_queue_item_id": "qi-report-source",
                "position": 0,
            }))
            .unwrap(),
            idempotency_key: Some("agent.defer:qi-report-source:0".to_owned()),
            caused_by: Some("attempt-report-completed".to_owned()),
            session_id: Some("session-report".to_owned()),
            run_id: Some("run-report".to_owned()),
            turn_id: None,
            timestamp_ms: 500,
            cursor: None,
        })
        .unwrap();
    let cancelled = dispatch
        .cancel_resource(
            "pub_cancel",
            None,
            Some("reporter"),
            Some("session-report"),
            RuntimeEventIdentity::new("deferred-cancel-terminal", 700).unwrap(),
        )
        .unwrap();
    assert_eq!(cancelled.resource_kind(), ResourceKind::DeferredPublication);
    assert_eq!(cancelled.status(), ResourceCancellationStatus::Cancelled);
    let cancelled_event = cancelled.event().unwrap();
    assert_eq!(
        cancelled_event.event_type,
        "runtime.deferred_publication.cancelled"
    );
    assert_eq!(
        cancelled_event.idempotency_key.as_deref(),
        Some("deferred_publication.cancelled:pub_cancel")
    );
    assert!(dispatch
        .publish_next_due_deferred_publication(1_000, &[])
        .unwrap()
        .is_none());
    assert_eq!(
        dispatch.list_deferred_publications().unwrap()[0].status(),
        DeferredPublicationStatus::Cancelled
    );

    let invalid = dispatch
        .cancel_resource(
            "other_cancel",
            None,
            None,
            None,
            RuntimeEventIdentity::new("invalid-cancel", 800).unwrap(),
        )
        .unwrap_err();
    assert_eq!(invalid.reason(), "invalid_cancellation_handle");
    let unknown = dispatch
        .cancel_resource(
            "wait_unknown",
            None,
            None,
            None,
            RuntimeEventIdentity::new("unknown-cancel", 801).unwrap(),
        )
        .unwrap_err();
    assert_eq!(unknown.reason(), "cancellation_resource_not_found");
}

#[test]
fn direct_session_message_cancels_wait_and_binds_queue_atomically() {
    let mut dispatch = Dispatch::open_in_memory().unwrap();
    let session_id = SessionId::from_str("session-direct").unwrap();
    dispatch
        .append_trusted_event(Event {
            id: "direct-wait-created".to_owned(),
            event_type: "runtime.wait.created".to_owned(),
            source: "zeta".to_owned(),
            payload: serde_json::from_value(json!({
                "handle": "wait_direct",
                "agent_id": "worker",
                "session_id": session_id,
                "event_type": "work.ready",
                "fields": {},
                "deadline": null,
                "source_queue_item_id": "qi-direct-prior",
                "project_revision": "revision-1",
            }))
            .unwrap(),
            idempotency_key: Some("agent.wait:qi-direct-prior:0".to_owned()),
            caused_by: Some("direct-prior-completed".to_owned()),
            session_id: Some(session_id.to_string()),
            run_id: Some("run-direct-prior".to_owned()),
            turn_id: None,
            timestamp_ms: 100,
            cursor: None,
        })
        .unwrap();
    let request = SessionMessageRequest::new(
        "continue now",
        "worker",
        session_id.clone(),
        "revision-2",
        RunId::from_str("run-direct").unwrap(),
    )
    .with_idempotency_key("session.message:session-direct:request-1");

    let submitted = dispatch
        .submit_session_message(
            &request,
            SessionMessageIdentities::new(
                RuntimeEventIdentity::new("direct-wait-cancelled", 200).unwrap(),
                RuntimeEventIdentity::new("direct-message-requested", 201).unwrap(),
                RuntimeEventIdentity::new("direct-message-available", 202).unwrap(),
            ),
        )
        .unwrap();
    assert!(submitted.changed());
    assert_eq!(submitted.event_id(), "direct-message-requested");
    assert_eq!(
        submitted.queue_item_id().as_str(),
        "qi_direct-message-requested_worker"
    );
    assert_eq!(submitted.agent_id(), "worker");
    assert_eq!(submitted.session_id(), &session_id);
    assert_eq!(submitted.run_id().as_str(), "run-direct");
    let event_types: Vec<_> = submitted
        .events()
        .iter()
        .map(|event| event.event_type.as_str())
        .collect();
    assert_eq!(
        event_types,
        [
            "runtime.wait.cancelled",
            "session.message.requested",
            "runtime.queue_item.available",
        ]
    );
    assert_eq!(
        submitted.events()[0].payload["reason"],
        "The user continued the session."
    );
    assert_eq!(
        Value::Object(submitted.events()[1].payload.clone()),
        json!({
            "message": "continue now",
            "agent_id": "worker",
            "session_id": "session-direct",
            "run_id": "run-direct",
        })
    );
    assert_eq!(
        submitted.events()[2].payload["project_revision"],
        "revision-2"
    );
    assert_eq!(
        dispatch.list_waits().unwrap()[0].status(),
        WaitStatus::Cancelled
    );
    assert_eq!(
        dispatch
            .queue_item(submitted.queue_item_id())
            .unwrap()
            .unwrap()
            .status(),
        QueueItemStatus::Available
    );

    let repeated = dispatch
        .submit_session_message(
            &request,
            SessionMessageIdentities::new(
                RuntimeEventIdentity::new("direct-wait-cancelled-retry", 300).unwrap(),
                RuntimeEventIdentity::new("direct-message-requested-retry", 301).unwrap(),
                RuntimeEventIdentity::new("direct-message-available-retry", 302).unwrap(),
            ),
        )
        .unwrap();
    assert!(!repeated.changed());
    assert_eq!(repeated.event_id(), "direct-message-requested");
    assert_eq!(repeated.events().len(), 2);
    assert_eq!(
        dispatch.list_events(&EventFilter::default()).unwrap().len(),
        4
    );
    dispatch.rebuild_projections().unwrap();
    assert_eq!(
        dispatch.list_waits().unwrap()[0].status(),
        WaitStatus::Cancelled
    );
    assert_eq!(
        dispatch
            .queue_item(submitted.queue_item_id())
            .unwrap()
            .unwrap()
            .status(),
        QueueItemStatus::Available
    );
}

#[test]
fn direct_session_message_owner_conflict_writes_nothing() {
    let mut dispatch = Dispatch::open_in_memory().unwrap();
    let session_id = SessionId::from_str("session-conflict").unwrap();
    dispatch
        .append_trusted_event(Event {
            id: "conflict-wait-created".to_owned(),
            event_type: "runtime.wait.created".to_owned(),
            source: "zeta".to_owned(),
            payload: serde_json::from_value(json!({
                "handle": "wait_conflict",
                "agent_id": "owner",
                "session_id": session_id,
                "event_type": "work.ready",
                "fields": {},
                "deadline": null,
                "source_queue_item_id": "qi-conflict-prior",
            }))
            .unwrap(),
            idempotency_key: Some("agent.wait:qi-conflict-prior:0".to_owned()),
            caused_by: Some("conflict-prior-completed".to_owned()),
            session_id: Some(session_id.to_string()),
            run_id: Some("run-conflict-prior".to_owned()),
            turn_id: None,
            timestamp_ms: 100,
            cursor: None,
        })
        .unwrap();
    let request = SessionMessageRequest::new(
        "continue",
        "intruder",
        session_id,
        "revision-2",
        RunId::from_str("run-conflict").unwrap(),
    );

    let error = dispatch
        .submit_session_message(
            &request,
            SessionMessageIdentities::new(
                RuntimeEventIdentity::new("conflict-wait-cancelled", 200).unwrap(),
                RuntimeEventIdentity::new("conflict-message", 201).unwrap(),
                RuntimeEventIdentity::new("conflict-available", 202).unwrap(),
            ),
        )
        .unwrap_err();

    assert_eq!(error.reason(), "cancellation_authority_mismatch");
    assert_eq!(
        dispatch.list_events(&EventFilter::default()).unwrap().len(),
        1
    );
    assert_eq!(
        dispatch.list_waits().unwrap()[0].status(),
        WaitStatus::Active
    );
    assert!(dispatch.list_queue_items().unwrap().is_empty());
}

#[test]
fn due_publication_consumes_pending_deferred_publication_once() {
    let case = scripted_case(
        "deferred_publications",
        "due_publication_consumes_pending_deferred_publication",
    );
    let mut dispatch = Dispatch::open_in_memory().unwrap();
    dispatch
        .append_trusted_event(runtime_event(&case["created_event"]))
        .unwrap();

    let published = dispatch
        .publish_next_due_deferred_publication(
            case["now_ms"].as_i64().unwrap(),
            &[
                RuntimeEventIdentity::new("deferred-publication-1", 2_000).unwrap(),
                RuntimeEventIdentity::new("deferred-published-fact-1", 2_001).unwrap(),
            ],
        )
        .unwrap()
        .unwrap();
    assert_eq!(published.len(), 2);
    assert!(dispatch
        .publish_next_due_deferred_publication(case["now_ms"].as_i64().unwrap(), &[])
        .unwrap()
        .is_none());

    let events = dispatch.list_events(&EventFilter::default()).unwrap();
    let expected = case["expected"]["events"].as_array().unwrap();
    assert_eq!(events.len(), expected.len());
    for (actual, expected) in events.iter().zip(expected) {
        assert_eq!(actual.event_type, expected["type"]);
        assert_eq!(
            actual.idempotency_key.as_deref(),
            expected["idempotency_key"].as_str()
        );
    }
    assert_eq!(events[1].caused_by.as_deref(), Some("deferred-created-1"));
    assert_eq!(
        Value::Object(events[1].payload.clone()),
        json!({"report_id": "publication-1"})
    );
    assert_eq!(
        events[2].caused_by.as_deref(),
        Some("deferred-publication-1")
    );
    assert_eq!(
        events[2].payload["published_event_id"],
        "deferred-publication-1"
    );

    let publications = dispatch.list_deferred_publications().unwrap();
    assert_eq!(publications.len(), 1);
    assert_eq!(
        publications[0].status(),
        DeferredPublicationStatus::Published
    );
    assert_eq!(
        publications[0].published_event_id(),
        Some("deferred-publication-1")
    );
}

#[test]
fn next_deadline_and_advance_due_are_durable_timer_primitives() {
    let mut dispatch = Dispatch::open_in_memory().unwrap();
    let wait = Event {
        id: "deadline-wait".to_owned(),
        event_type: "runtime.wait.created".to_owned(),
        source: "zeta".to_owned(),
        payload: json!({
            "handle": "wait_deadline",
            "agent_id": "worker",
            "session_id": "agent/worker",
            "event_type": "example.done",
            "fields": {},
            "deadline": "1970-01-01T00:00:02Z",
            "source_queue_item_id": "qi_deadline_source",
            "project_revision": "project:test"
        })
        .as_object()
        .unwrap()
        .clone(),
        idempotency_key: Some("wait.created:wait_deadline".to_owned()),
        caused_by: Some("deadline-source".to_owned()),
        session_id: Some("agent/worker".to_owned()),
        run_id: None,
        turn_id: None,
        timestamp_ms: 1,
        cursor: None,
    };
    dispatch.append_trusted_event(wait).unwrap();
    assert_eq!(dispatch.next_deadline_ms(1_000).unwrap(), Some(2_000));
    assert!(dispatch.has_due_maintenance(2_000).unwrap());

    let mut sequence = 0_u64;
    let events = dispatch
        .advance_due(2_000, 8, || {
            sequence += 1;
            RuntimeEventIdentity::new(format!("deadline-runtime-{sequence}"), 2_000).map_err(
                |error| DispatchError::InvalidCoordinationInput {
                    field: error.resource(),
                },
            )
        })
        .unwrap();
    assert_eq!(
        events
            .iter()
            .map(|event| event.event_type.as_str())
            .collect::<Vec<_>>(),
        vec!["runtime.wait.timed_out", "runtime.queue_item.available"]
    );
    assert_eq!(dispatch.next_deadline_ms(2_000).unwrap(), None);
    assert!(!dispatch.has_due_maintenance(2_000).unwrap());
}

#[test]
fn next_deadline_includes_future_queue_availability() {
    let mut dispatch = Dispatch::open_in_memory().unwrap();
    let input = Event {
        id: "queue-deadline-input".to_owned(),
        event_type: "example.created".to_owned(),
        source: "test".to_owned(),
        payload: Map::new(),
        idempotency_key: Some("queue-deadline-input".to_owned()),
        caused_by: None,
        session_id: None,
        run_id: None,
        turn_id: None,
        timestamp_ms: 1_000,
        cursor: None,
    };
    dispatch.ingest_event(input.clone()).unwrap();
    let queue_item_id = queue_item_id(&input.id, "worker");
    dispatch
        .append_trusted_event(Event {
            id: "queue-deadline-available".to_owned(),
            event_type: "runtime.queue_item.available".to_owned(),
            source: "zeta".to_owned(),
            payload: json!({
                "queue_item_id": queue_item_id,
                "event_id": input.id,
                "target_agent": "worker",
                "status": "available",
                "not_before": 2_000,
            })
            .as_object()
            .unwrap()
            .clone(),
            idempotency_key: Some("queue_item:queue-deadline-input:worker:available".to_owned()),
            caused_by: Some("queue-deadline-input".to_owned()),
            session_id: None,
            run_id: None,
            turn_id: None,
            timestamp_ms: 1_000,
            cursor: None,
        })
        .unwrap();

    assert_eq!(dispatch.next_deadline_ms(1_000).unwrap(), Some(2_000));
    assert_eq!(dispatch.next_deadline_ms(2_000).unwrap(), None);
}

#[test]
fn due_publication_and_matching_waits_commit_or_roll_back_together() {
    let mut dispatch = Dispatch::open_in_memory().unwrap();
    dispatch
        .append_trusted_event(Event {
            id: "publication-wait-created".to_owned(),
            event_type: "runtime.wait.created".to_owned(),
            source: "zeta".to_owned(),
            payload: serde_json::from_value(json!({
                "handle": "wait_publication",
                "agent_id": "consumer",
                "session_id": "session-consumer",
                "event_type": "report.ready",
                "fields": {"report_id": "atomic"},
                "deadline": null,
                "source_queue_item_id": "qi-consumer-source",
            }))
            .unwrap(),
            idempotency_key: Some("agent.wait:qi-consumer-source:0".to_owned()),
            caused_by: Some("consumer-attempt-completed".to_owned()),
            session_id: Some("session-consumer".to_owned()),
            run_id: Some("run-consumer".to_owned()),
            turn_id: None,
            timestamp_ms: 100,
            cursor: None,
        })
        .unwrap();
    dispatch
        .append_trusted_event(Event {
            id: "publication-created".to_owned(),
            event_type: "runtime.deferred_publication.created".to_owned(),
            source: "zeta".to_owned(),
            payload: serde_json::from_value(json!({
                "handle": "pub_atomic",
                "event_type": "report.ready",
                "payload": {"report_id": "atomic"},
                "publish_at": "1970-01-01T00:00:01+00:00",
                "source_agent_id": "reporter",
                "source_session_id": "session-reporter",
                "source_queue_item_id": "qi-reporter-source",
                "position": 0,
            }))
            .unwrap(),
            idempotency_key: Some("agent.defer:qi-reporter-source:0".to_owned()),
            caused_by: Some("reporter-attempt-completed".to_owned()),
            session_id: Some("session-reporter".to_owned()),
            run_id: Some("run-reporter".to_owned()),
            turn_id: None,
            timestamp_ms: 101,
            cursor: None,
        })
        .unwrap();
    let mut collision = journal_event("publication-terminal-collision", None);
    collision.event_type = "zeta.test".to_owned();
    dispatch.append_trusted_event(collision).unwrap();

    let error = dispatch
        .publish_next_due_deferred_publication(
            1_000,
            &[
                RuntimeEventIdentity::new("publication-event-rollback", 200).unwrap(),
                RuntimeEventIdentity::new("publication-wait-match-rollback", 201).unwrap(),
                RuntimeEventIdentity::new("publication-continuation-rollback", 202).unwrap(),
                RuntimeEventIdentity::new("publication-terminal-collision", 203).unwrap(),
            ],
        )
        .unwrap_err();

    assert_eq!(error.reason(), "runtime_event_identity_collision");
    assert_eq!(
        dispatch.list_events(&EventFilter::default()).unwrap().len(),
        3
    );
    assert_eq!(
        dispatch.list_waits().unwrap()[0].status(),
        WaitStatus::Active
    );
    assert_eq!(
        dispatch.list_deferred_publications().unwrap()[0].status(),
        DeferredPublicationStatus::Pending
    );
    assert!(dispatch.list_queue_items().unwrap().is_empty());

    let events = dispatch
        .publish_next_due_deferred_publication(
            1_000,
            &[
                RuntimeEventIdentity::new("publication-event", 300).unwrap(),
                RuntimeEventIdentity::new("publication-wait-match", 301).unwrap(),
                RuntimeEventIdentity::new("publication-continuation", 302).unwrap(),
                RuntimeEventIdentity::new("publication-terminal", 303).unwrap(),
            ],
        )
        .unwrap()
        .unwrap();
    let mut event_types = Vec::new();
    for event in &events {
        event_types.push(event.event_type.as_str());
    }
    assert_eq!(
        event_types,
        [
            "report.ready",
            "runtime.wait.matched",
            "runtime.queue_item.available",
            "runtime.deferred_publication.published",
        ]
    );
    assert_eq!(
        dispatch.list_waits().unwrap()[0].status(),
        WaitStatus::Matched
    );
    assert_eq!(
        dispatch.list_deferred_publications().unwrap()[0].status(),
        DeferredPublicationStatus::Published
    );
    let queue_items = dispatch.list_queue_items().unwrap();
    assert_eq!(queue_items.len(), 2);
    assert_eq!(queue_items[0].status(), QueueItemStatus::Pending);
    assert_eq!(queue_items[1].status(), QueueItemStatus::Available);
}

#[derive(Clone, Copy)]
enum EffectResultShape {
    Absent,
    Object,
    Scalar,
}

struct EffectProjectionCase {
    name: &'static str,
    semantics: &'static str,
    expected_semantics: EffectDeliverySemantics,
    lifecycle: &'static [(&'static str, EffectResultShape)],
    expected_status: EffectStatus,
}

struct InvalidEffectProjectionCase {
    name: &'static str,
    semantics: &'static str,
    lifecycle: &'static [(&'static str, EffectResultShape, Option<&'static str>)],
    expected_field: &'static str,
}

struct EffectEventFields<'a> {
    name: &'a str,
    key: &'a str,
    semantics: &'a str,
    status: &'a str,
    result_shape: EffectResultShape,
    payload_status: Option<&'a str>,
    position: usize,
}

fn effect_result_value(name: &str, status: &str, result_shape: EffectResultShape) -> Option<Value> {
    match result_shape {
        EffectResultShape::Absent => None,
        EffectResultShape::Object => Some(json!({"case": name, "status": status})),
        EffectResultShape::Scalar => Some(json!("invalid result")),
    }
}

fn effect_projection_event(fields: EffectEventFields<'_>) -> Event {
    let EffectEventFields {
        name,
        key,
        semantics,
        status,
        result_shape,
        payload_status,
        position,
    } = fields;
    let operation = format!("test.{name}");
    let mut payload: Map<String, Value> = serde_json::from_value(json!({
        "effect_key": key,
        "operation": operation,
        "semantics": semantics,
        "scope": format!("scope-{name}"),
        "params": {"case": name},
        "status": payload_status.unwrap_or(status),
    }))
    .unwrap();
    if let Some(result) = effect_result_value(name, status, result_shape) {
        payload.insert("result".to_owned(), result);
    }
    Event {
        id: format!("effect-{name}-{position}"),
        event_type: format!("runtime.effect.{status}"),
        source: "capability:test.effect".to_owned(),
        payload,
        idempotency_key: Some(format!("runtime.effect.{status}:{key}")),
        caused_by: Some(format!("attempt-{name}")),
        session_id: None,
        run_id: None,
        turn_id: None,
        timestamp_ms: 10_000 + position as i64,
        cursor: None,
    }
}

#[test]
fn effect_projection_codecs_transitions_and_rebuild_are_stable() {
    let cases = [
        EffectProjectionCase {
            name: "planned",
            semantics: "idempotent_with_key",
            expected_semantics: EffectDeliverySemantics::IdempotentWithKey,
            lifecycle: &[("planned", EffectResultShape::Absent)],
            expected_status: EffectStatus::Planned,
        },
        EffectProjectionCase {
            name: "started",
            semantics: "connector_deduplicated",
            expected_semantics: EffectDeliverySemantics::ConnectorDeduplicated,
            lifecycle: &[
                ("planned", EffectResultShape::Absent),
                ("started", EffectResultShape::Absent),
            ],
            expected_status: EffectStatus::Started,
        },
        EffectProjectionCase {
            name: "completed",
            semantics: "at_least_once",
            expected_semantics: EffectDeliverySemantics::AtLeastOnce,
            lifecycle: &[
                ("planned", EffectResultShape::Absent),
                ("started", EffectResultShape::Absent),
                ("completed", EffectResultShape::Object),
            ],
            expected_status: EffectStatus::Completed,
        },
        EffectProjectionCase {
            name: "failed",
            semantics: "idempotent_with_key",
            expected_semantics: EffectDeliverySemantics::IdempotentWithKey,
            lifecycle: &[
                ("planned", EffectResultShape::Absent),
                ("started", EffectResultShape::Absent),
                ("failed", EffectResultShape::Object),
            ],
            expected_status: EffectStatus::Failed,
        },
        EffectProjectionCase {
            name: "ambiguous",
            semantics: "unsafe_to_retry",
            expected_semantics: EffectDeliverySemantics::UnsafeToRetry,
            lifecycle: &[
                ("planned", EffectResultShape::Absent),
                ("started", EffectResultShape::Absent),
                ("ambiguous", EffectResultShape::Object),
            ],
            expected_status: EffectStatus::Ambiguous,
        },
    ];
    let mut dispatch = Dispatch::open_in_memory().unwrap();
    for EffectProjectionCase {
        name,
        semantics,
        expected_semantics,
        lifecycle,
        expected_status,
    } in cases
    {
        let operation = format!("test.{name}");
        let params = serde_json::from_value(json!({"case": name})).unwrap();
        let key = effect_key(&format!("scope-{name}"), &operation, &params).unwrap();
        for (position, &(status, result_shape)) in lifecycle.iter().enumerate() {
            dispatch
                .append_trusted_event(effect_projection_event(EffectEventFields {
                    name,
                    key: &key,
                    semantics,
                    status,
                    result_shape,
                    payload_status: None,
                    position,
                }))
                .unwrap();
        }
        let effects = dispatch.list_effects().unwrap();
        let effect = &effects[effects.len() - 1];
        assert_eq!(effect.key(), key);
        assert_eq!(effect.semantics(), expected_semantics);
        assert_eq!(effect.status(), expected_status);
        let &(terminal_status, result_shape) = lifecycle.last().unwrap();
        let expected_result = effect_result_value(name, terminal_status, result_shape)
            .and_then(|value| value.as_object().cloned());
        assert_eq!(effect.result(), expected_result.as_ref());
        let terminal = match expected_status {
            EffectStatus::Planned => false,
            EffectStatus::Started => false,
            EffectStatus::Completed => true,
            EffectStatus::Failed => true,
            EffectStatus::Ambiguous => true,
        };
        assert_eq!(effect.terminal_event_id().is_some(), terminal);
    }
    let before_rebuild = dispatch.list_effects().unwrap();
    dispatch.rebuild_projections().unwrap();
    assert_eq!(dispatch.list_effects().unwrap(), before_rebuild);

    let invalid_cases = [
        InvalidEffectProjectionCase {
            name: "unknown-semantics",
            semantics: "unknown",
            lifecycle: &[("planned", EffectResultShape::Absent, None)],
            expected_field: "semantics",
        },
        InvalidEffectProjectionCase {
            name: "unknown-status",
            semantics: "idempotent_with_key",
            lifecycle: &[("unknown", EffectResultShape::Absent, None)],
            expected_field: "status",
        },
        InvalidEffectProjectionCase {
            name: "status-mismatch",
            semantics: "idempotent_with_key",
            lifecycle: &[("planned", EffectResultShape::Absent, Some("started"))],
            expected_field: "status",
        },
        InvalidEffectProjectionCase {
            name: "nonterminal-result",
            semantics: "idempotent_with_key",
            lifecycle: &[("planned", EffectResultShape::Object, None)],
            expected_field: "result",
        },
        InvalidEffectProjectionCase {
            name: "missing-terminal-result",
            semantics: "idempotent_with_key",
            lifecycle: &[
                ("planned", EffectResultShape::Absent, None),
                ("started", EffectResultShape::Absent, None),
                ("completed", EffectResultShape::Absent, None),
            ],
            expected_field: "result",
        },
        InvalidEffectProjectionCase {
            name: "scalar-terminal-result",
            semantics: "idempotent_with_key",
            lifecycle: &[
                ("planned", EffectResultShape::Absent, None),
                ("started", EffectResultShape::Absent, None),
                ("completed", EffectResultShape::Scalar, None),
            ],
            expected_field: "result",
        },
        InvalidEffectProjectionCase {
            name: "illegal-transition",
            semantics: "idempotent_with_key",
            lifecycle: &[
                ("planned", EffectResultShape::Absent, None),
                ("completed", EffectResultShape::Object, None),
            ],
            expected_field: "status",
        },
        InvalidEffectProjectionCase {
            name: "unsafe-failure",
            semantics: "unsafe_to_retry",
            lifecycle: &[
                ("planned", EffectResultShape::Absent, None),
                ("started", EffectResultShape::Absent, None),
                ("failed", EffectResultShape::Object, None),
            ],
            expected_field: "status",
        },
        InvalidEffectProjectionCase {
            name: "retry-safe-ambiguity",
            semantics: "at_least_once",
            lifecycle: &[
                ("planned", EffectResultShape::Absent, None),
                ("started", EffectResultShape::Absent, None),
                ("ambiguous", EffectResultShape::Object, None),
            ],
            expected_field: "semantics",
        },
    ];
    for InvalidEffectProjectionCase {
        name,
        semantics,
        lifecycle,
        expected_field,
    } in invalid_cases
    {
        let mut dispatch = Dispatch::open_in_memory().unwrap();
        let operation = format!("test.{name}");
        let params = serde_json::from_value(json!({"case": name})).unwrap();
        let key = effect_key(&format!("scope-{name}"), &operation, &params).unwrap();
        let Some((candidate, prelude)) = lifecycle.split_last() else {
            panic!("invalid effect case must include a candidate");
        };
        for (position, &(status, result_shape, payload_status)) in prelude.iter().enumerate() {
            dispatch
                .append_trusted_event(effect_projection_event(EffectEventFields {
                    name,
                    key: &key,
                    semantics,
                    status,
                    result_shape,
                    payload_status,
                    position,
                }))
                .unwrap();
        }
        let retained_events = dispatch.list_events(&EventFilter::default()).unwrap();
        let retained_effects = dispatch.list_effects().unwrap();
        let &(status, result_shape, payload_status) = candidate;
        let error = dispatch
            .append_trusted_event(effect_projection_event(EffectEventFields {
                name,
                key: &key,
                semantics,
                status,
                result_shape,
                payload_status,
                position: prelude.len(),
            }))
            .unwrap_err();
        let DispatchError::InvalidLifecycleEvent { event_id, field } = error else {
            panic!("unexpected invalid effect error: {error}");
        };
        assert_eq!(event_id, format!("effect-{name}-{}", prelude.len()));
        assert_eq!(field, expected_field);
        assert_eq!(
            dispatch.list_events(&EventFilter::default()).unwrap(),
            retained_events
        );
        assert_eq!(dispatch.list_effects().unwrap(), retained_effects);
    }
}

#[test]
fn unsafe_effect_failure_projects_as_a_retry_blocker() {
    let case = scripted_case("effects", "unsafe_failure_becomes_ambiguous");
    let mut dispatch = Dispatch::open_in_memory().unwrap();
    let effect_key = effect_key(
        case["scope"].as_str().unwrap(),
        case["operation"].as_str().unwrap(),
        case["params"].as_object().unwrap(),
    )
    .unwrap();

    for (position, action) in case["actions"].as_array().unwrap().iter().enumerate() {
        let status = action["status"].as_str().unwrap();
        let mut payload = Map::new();
        payload.insert("effect_key".to_owned(), Value::String(effect_key.clone()));
        payload.insert("operation".to_owned(), case["operation"].clone());
        payload.insert("semantics".to_owned(), case["semantics"].clone());
        payload.insert("scope".to_owned(), case["scope"].clone());
        payload.insert("queue_item_id".to_owned(), case["scope"].clone());
        payload.insert("params".to_owned(), case["params"].clone());
        payload.insert("status".to_owned(), action["status"].clone());
        if let Some(result) = action.get("result") {
            payload.insert("result".to_owned(), result.clone());
        }
        dispatch
            .append_trusted_event(Event {
                id: format!("effect-event-{position}"),
                event_type: format!("runtime.effect.{status}"),
                source: "capability:test.bash".to_owned(),
                payload,
                idempotency_key: Some(format!("runtime.effect.{status}:{effect_key}")),
                caused_by: case["caused_by"].as_str().map(str::to_owned),
                session_id: None,
                run_id: None,
                turn_id: None,
                timestamp_ms: 700 + position as i64,
                cursor: None,
            })
            .unwrap();
    }

    let expected = case["expected"].as_array().unwrap();
    let events = dispatch.list_events(&EventFilter::default()).unwrap();
    assert_eq!(events.len(), expected.len());
    for (actual, expected) in events.iter().zip(expected) {
        assert_eq!(actual.event_type, expected["type"]);
        assert_eq!(actual.caused_by.as_deref(), expected["caused_by"].as_str());
        assert_eq!(actual.payload["effect_key"], effect_key);
        assert_eq!(actual.payload["status"], expected["payload"]["status"]);
    }

    let effects = dispatch.list_effects().unwrap();
    assert_eq!(effects.len(), 1);
    assert_eq!(effects[0].key(), effect_key);
    assert_eq!(effects[0].status(), EffectStatus::Ambiguous);
    assert_eq!(effects[0].result(), action_result(&case));
    let queue_item_id = QueueItemId::from_str(case["scope"].as_str().unwrap()).unwrap();
    assert_eq!(
        dispatch.blocking_unsafe_effect(&queue_item_id).unwrap(),
        Some(effect_key.clone())
    );

    dispatch.rebuild_projections().unwrap();
    assert_eq!(
        dispatch.list_effects().unwrap()[0].status(),
        EffectStatus::Ambiguous
    );
    assert_eq!(
        dispatch.blocking_unsafe_effect(&queue_item_id).unwrap(),
        Some(effect_key)
    );
}

fn action_result(case: &Value) -> Option<&Map<String, Value>> {
    case["actions"].as_array().unwrap().last().unwrap()["result"].as_object()
}

#[test]
fn effect_key_is_canonical_across_parameter_order() {
    let first = Map::from_iter([
        ("channel".to_owned(), json!("C1")),
        ("message".to_owned(), json!({"text": "hello", "blocks": []})),
    ]);
    let second = Map::from_iter([
        ("message".to_owned(), json!({"blocks": [], "text": "hello"})),
        ("channel".to_owned(), json!("C1")),
    ]);

    let first = effect_key("qi_1", "slack.post_message", &first).unwrap();
    let second = effect_key("qi_1", "slack.post_message", &second).unwrap();
    assert_eq!(first, second);
    assert!(first.starts_with("effect:b3:"));
}

#[test]
fn session_reads_follow_running_queued_waiting_idle_priority() {
    let case = scripted_case("session_reads", "running_queued_waiting_idle_priority");
    let mut dispatch = Dispatch::open_in_memory().unwrap();
    append_session_queue(
        &mut dispatch,
        "running",
        "session-running",
        &[
            (QueueItemStatus::Available, 35),
            (QueueItemStatus::Claimed, 40),
        ],
    );
    append_session_queue(
        &mut dispatch,
        "behind",
        "session-running",
        &[(QueueItemStatus::Available, 50)],
    );
    append_session_queue(
        &mut dispatch,
        "queued",
        "session-queued",
        &[
            (QueueItemStatus::Available, 26),
            (QueueItemStatus::Claimed, 27),
            (QueueItemStatus::Failed, 28),
            (QueueItemStatus::RetryScheduled, 30),
        ],
    );
    append_session_queue(
        &mut dispatch,
        "waiting",
        "session-waiting",
        &[
            (QueueItemStatus::Available, 17),
            (QueueItemStatus::Claimed, 18),
            (QueueItemStatus::Completed, 20),
        ],
    );
    append_session_queue(
        &mut dispatch,
        "idle",
        "session-idle",
        &[
            (QueueItemStatus::Available, 7),
            (QueueItemStatus::Claimed, 8),
            (QueueItemStatus::Completed, 10),
        ],
    );
    append_session_cancellation_request(&mut dispatch);
    append_session_running_attempt(&mut dispatch);
    append_session_wait(&mut dispatch);

    let sessions = dispatch.list_sessions().unwrap();
    assert_eq!(serde_json::to_value(&sessions).unwrap(), case["expected"]);
    let running_id = SessionId::from_str("session-running").unwrap();
    assert_eq!(
        serde_json::to_value(dispatch.session_status(&running_id).unwrap()).unwrap(),
        case["expected"][0]
    );
    let before_rebuild = sessions;
    dispatch.rebuild_projections().unwrap();
    assert_eq!(dispatch.list_sessions().unwrap(), before_rebuild);
}

fn append_session_queue(
    dispatch: &mut Dispatch,
    event_id: &str,
    session_id: &str,
    transitions: &[(QueueItemStatus, i64)],
) {
    append_session_queue_for_agent(dispatch, event_id, "worker", session_id, transitions);
}

fn append_session_queue_for_agent(
    dispatch: &mut Dispatch,
    event_id: &str,
    agent_id: &str,
    session_id: &str,
    transitions: &[(QueueItemStatus, i64)],
) {
    dispatch
        .ingest_event(Event {
            id: event_id.to_owned(),
            event_type: "session.work".to_owned(),
            source: "test".to_owned(),
            payload: Map::new(),
            idempotency_key: Some(format!("session-input:{event_id}")),
            caused_by: None,
            session_id: None,
            run_id: None,
            turn_id: None,
            timestamp_ms: 1,
            cursor: None,
        })
        .unwrap();
    for (position, (status, timestamp_ms)) in transitions.iter().enumerate() {
        let suffix = status.to_string();
        let mut payload = Map::new();
        payload.insert(
            "queue_item_id".to_owned(),
            Value::String(format!("qi_{event_id}")),
        );
        payload.insert("event_id".to_owned(), Value::String(event_id.to_owned()));
        payload.insert(
            "target_agent".to_owned(),
            Value::String(agent_id.to_owned()),
        );
        payload.insert("status".to_owned(), Value::String(suffix.clone()));
        payload.insert(
            "session_id".to_owned(),
            Value::String(session_id.to_owned()),
        );
        dispatch
            .append_trusted_event(Event {
                id: format!("session-{event_id}-{suffix}-{position}"),
                event_type: format!("runtime.queue_item.{suffix}"),
                source: "zeta".to_owned(),
                payload,
                idempotency_key: Some(format!("session-queue:{event_id}:{suffix}:{position}")),
                caused_by: Some(event_id.to_owned()),
                session_id: Some(session_id.to_owned()),
                run_id: None,
                turn_id: None,
                timestamp_ms: *timestamp_ms,
                cursor: None,
            })
            .unwrap();
    }
}

#[test]
fn session_status_rejects_unknown_and_conflicting_owners() {
    let mut dispatch = Dispatch::open_in_memory().unwrap();
    append_session_queue_for_agent(
        &mut dispatch,
        "conflict-a",
        "agent-z",
        "session-conflict",
        &[
            (QueueItemStatus::Available, 1),
            (QueueItemStatus::Claimed, 2),
            (QueueItemStatus::Completed, 3),
        ],
    );
    append_session_queue_for_agent(
        &mut dispatch,
        "conflict-b",
        "agent-a",
        "session-conflict",
        &[
            (QueueItemStatus::Available, 4),
            (QueueItemStatus::Claimed, 5),
            (QueueItemStatus::Completed, 6),
        ],
    );

    let sessions = dispatch.list_sessions().unwrap();
    assert_eq!(sessions.len(), 1);
    assert_eq!(sessions[0].agent_id(), None);
    assert_eq!(
        sessions[0].conflicting_agent_ids(),
        ["agent-a".to_owned(), "agent-z".to_owned()]
    );
    let session_id = SessionId::from_str("session-conflict").unwrap();
    assert_eq!(
        dispatch.session_status(&session_id).unwrap_err().reason(),
        "session_owner_conflict"
    );
    let unknown = SessionId::from_str("session-unknown").unwrap();
    assert_eq!(
        dispatch.session_status(&unknown).unwrap_err().reason(),
        "session_not_found"
    );
}

fn append_session_cancellation_request(dispatch: &mut Dispatch) {
    let mut payload = Map::new();
    payload.insert(
        "queue_item_id".to_owned(),
        Value::String("qi_running".to_owned()),
    );
    payload.insert("event_id".to_owned(), Value::String("running".to_owned()));
    payload.insert(
        "target_agent".to_owned(),
        Value::String("worker".to_owned()),
    );
    payload.insert("status".to_owned(), Value::String("claimed".to_owned()));
    dispatch
        .append_trusted_event(Event {
            id: "evt_cancel_request".to_owned(),
            event_type: "runtime.queue_item.cancel_requested".to_owned(),
            source: "zeta".to_owned(),
            payload,
            idempotency_key: Some("session-cancel:qi_running".to_owned()),
            caused_by: Some("running".to_owned()),
            session_id: Some("session-running".to_owned()),
            run_id: None,
            turn_id: None,
            timestamp_ms: 45,
            cursor: None,
        })
        .unwrap();
}

fn append_session_running_attempt(dispatch: &mut Dispatch) {
    let mut payload = Map::new();
    payload.insert(
        "attempt_id".to_owned(),
        Value::String("att_running".to_owned()),
    );
    payload.insert(
        "queue_item_id".to_owned(),
        Value::String("qi_running".to_owned()),
    );
    payload.insert("event_id".to_owned(), Value::String("running".to_owned()));
    payload.insert("attempt_number".to_owned(), Value::from(1));
    payload.insert(
        "target_agent".to_owned(),
        Value::String("worker".to_owned()),
    );
    payload.insert("status".to_owned(), Value::String("running".to_owned()));
    payload.insert(
        "started_at".to_owned(),
        Value::String("2026-08-06T10:00:00+00:00".to_owned()),
    );
    payload.insert(
        "session_id".to_owned(),
        Value::String("session-running".to_owned()),
    );
    payload.insert("run_id".to_owned(), Value::String("run_running".to_owned()));
    dispatch
        .append_trusted_event(Event {
            id: "session-attempt-running".to_owned(),
            event_type: "runtime.attempt.started".to_owned(),
            source: "zeta".to_owned(),
            payload,
            idempotency_key: Some("attempt:qi_running:1:running".to_owned()),
            caused_by: Some("running".to_owned()),
            session_id: Some("session-running".to_owned()),
            run_id: Some("run_running".to_owned()),
            turn_id: None,
            timestamp_ms: 41,
            cursor: None,
        })
        .unwrap();
}

fn append_session_wait(dispatch: &mut Dispatch) {
    let mut payload = Map::new();
    payload.insert("handle".to_owned(), Value::String("wait_active".to_owned()));
    payload.insert("agent_id".to_owned(), Value::String("worker".to_owned()));
    payload.insert(
        "session_id".to_owned(),
        Value::String("session-waiting".to_owned()),
    );
    payload.insert(
        "event_type".to_owned(),
        Value::String("work.ready".to_owned()),
    );
    payload.insert("fields".to_owned(), json!({"work_id": 7}));
    payload.insert("deadline".to_owned(), Value::Null);
    payload.insert(
        "source_queue_item_id".to_owned(),
        Value::String("qi_waiting".to_owned()),
    );
    payload.insert("project_revision".to_owned(), Value::Null);
    dispatch
        .append_trusted_event(Event {
            id: "session-wait-active".to_owned(),
            event_type: "runtime.wait.created".to_owned(),
            source: "zeta".to_owned(),
            payload,
            idempotency_key: Some("agent.wait:qi_waiting:0".to_owned()),
            caused_by: Some("session-attempt-completed".to_owned()),
            session_id: Some("session-waiting".to_owned()),
            run_id: None,
            turn_id: None,
            timestamp_ms: 25,
            cursor: None,
        })
        .unwrap();
}
