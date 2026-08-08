use serde::{Deserialize, Serialize};
use serde_json::Value;

#[derive(Clone, Copy, Debug, Deserialize, Hash, PartialEq, Eq, Serialize)]
#[serde(transparent)]
pub(super) struct RequestId(pub(super) u64);

#[derive(Debug, Serialize)]
pub(super) struct RpcRequest<'a> {
    jsonrpc: &'static str,
    id: RequestId,
    method: &'a str,
    params: Value,
}

impl<'a> RpcRequest<'a> {
    pub(super) fn new(id: RequestId, method: &'a str, params: Value) -> Self {
        Self {
            jsonrpc: "2.0",
            id,
            method,
            params,
        }
    }
}

#[derive(Debug, Deserialize)]
#[serde(untagged)]
pub(super) enum IncomingMessage {
    Success(RpcSuccess),
    Failure(RpcFailure),
    Notification(RpcNotification),
}

#[derive(Debug, Deserialize)]
pub(super) struct RpcSuccess {
    pub(super) jsonrpc: String,
    pub(super) id: RequestId,
    pub(super) result: Value,
}

#[derive(Debug, Deserialize)]
pub(super) struct RpcFailure {
    pub(super) jsonrpc: String,
    pub(super) id: RequestId,
    pub(super) error: RpcError,
}

#[derive(Debug, Deserialize)]
pub(super) struct RpcError {
    pub(super) code: i64,
    pub(super) message: String,
    pub(super) data: Option<Value>,
}

#[derive(Debug, Deserialize)]
pub(super) struct RpcNotification {
    pub(super) jsonrpc: String,
    pub(super) method: String,
    pub(super) params: Value,
}

#[derive(Debug, Deserialize, PartialEq, Eq, Serialize)]
#[serde(transparent)]
struct EventId(String);

#[derive(Debug, Deserialize, PartialEq, Eq, Serialize)]
#[serde(transparent)]
struct SessionId(String);

#[derive(Debug, Deserialize, PartialEq, Eq, Serialize)]
#[serde(transparent)]
struct RunId(String);

#[derive(Debug, Deserialize, PartialEq, Eq, Serialize)]
#[serde(transparent)]
struct TurnId(String);

#[derive(Clone, Copy, Debug, Deserialize, PartialEq, Eq, Serialize)]
#[serde(transparent)]
pub(super) struct Cursor(pub(super) u64);

#[derive(Debug, Deserialize, Serialize)]
pub(super) struct Event {
    id: EventId,
    event_type: String,
    source: String,
    payload: Value,
    idempotency_key: Option<String>,
    caused_by: Option<EventId>,
    session_id: Option<SessionId>,
    run_id: Option<RunId>,
    turn_id: Option<TurnId>,
    timestamp_ms: i64,
    cursor: Option<Cursor>,
}

impl Event {
    pub(super) fn id(&self) -> &str {
        &self.id.0
    }

    pub(super) fn idempotency_key(&self) -> Option<&str> {
        self.idempotency_key.as_deref()
    }

    pub(super) fn event_type(&self) -> &str {
        &self.event_type
    }

    pub(super) fn timeline_text(&self) -> String {
        if let Some(message) = self.payload.get("message").and_then(Value::as_str) {
            return message.to_owned();
        }
        if let Some(content) = self.payload.get("content").and_then(Value::as_str) {
            return content.to_owned();
        }
        self.payload.to_string()
    }

    pub(super) fn payload(&self) -> &Value {
        &self.payload
    }

    pub(super) fn run_id(&self) -> Option<&str> {
        let run_id = self.run_id.as_ref()?;
        Some(&run_id.0)
    }

    pub(super) fn belongs_to_session(&self, session_id: &str) -> bool {
        let Some(event_session_id) = &self.session_id else {
            return false;
        };
        event_session_id.0 == session_id
    }

    pub(super) fn is_direct_message_request(&self) -> bool {
        self.event_type == "session.message.requested"
    }

    pub(super) fn is_runtime_user_message(&self) -> bool {
        self.event_type == "zeta.user_message"
    }

    pub(super) fn user_message_key(&self) -> Option<(&str, &str, &str)> {
        let session_id = self.session_id.as_ref()?;
        let run_id = self.run_id.as_ref()?;
        let text = if self.is_direct_message_request() {
            self.payload.get("message").and_then(Value::as_str)?
        } else if self.is_runtime_user_message() {
            self.payload.get("content").and_then(Value::as_str)?
        } else {
            return None;
        };
        Some((&session_id.0, &run_id.0, text))
    }
}

#[derive(Debug, Deserialize)]
pub(super) struct EventsListResult {
    pub(super) events: Vec<Event>,
    pub(super) next_cursor: Option<Cursor>,
}

#[derive(Debug, Deserialize)]
pub(super) struct InitializeResult {
    pub(super) server: String,
    pub(super) protocol: String,
}

#[derive(Debug, Deserialize)]
pub(super) struct Session {
    session_id: SessionId,
    agent_id: Option<String>,
    status: String,
}

impl Session {
    pub(super) fn session_id(&self) -> &str {
        &self.session_id.0
    }

    pub(super) fn agent_id(&self) -> &str {
        match &self.agent_id {
            Some(agent_id) => agent_id,
            None => "unknown agent",
        }
    }

    pub(super) fn status(&self) -> &str {
        &self.status
    }
}

#[derive(Debug, Deserialize)]
pub(super) struct SessionsListResult {
    pub(super) sessions: Vec<Session>,
}

#[derive(Debug, Deserialize)]
pub(super) struct SubmitResult {
    pub(super) event_id: String,
    pub(super) session_id: String,
    pub(super) status: String,
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::{
        Cursor, Event, EventId, EventsListResult, IncomingMessage, InitializeResult, RequestId,
        RpcRequest, RunId, SessionId, SessionsListResult, SubmitResult,
    };

    #[test]
    fn event_accepts_current_wire_shape_and_unknown_fields() {
        let message = r#"
        {
          "id": "evt_123",
          "event_type": "future.event.type",
          "source": "test",
          "payload": {"message": "hello", "future": {"value": 1}},
          "idempotency_key": null,
          "caused_by": null,
          "session_id": "session_123",
          "run_id": "run_123",
          "turn_id": null,
          "timestamp_ms": 1754438400000,
          "cursor": 42,
          "future_top_level_field": true
        }
        "#;

        let event: Event = serde_json::from_str(message).expect("event should parse");

        assert_eq!(event.id, EventId("evt_123".to_owned()));
        assert_eq!(event.event_type, "future.event.type");
        assert_eq!(event.source, "test");
        assert_eq!(event.payload["message"], "hello");
        assert_eq!(event.idempotency_key, None);
        assert_eq!(event.caused_by, None);
        assert_eq!(event.session_id, Some(SessionId("session_123".to_owned())));
        assert_eq!(event.run_id, Some(RunId("run_123".to_owned())));
        assert_eq!(event.turn_id, None);
        assert_eq!(event.timestamp_ms, 1_754_438_400_000);
        assert_eq!(event.cursor, Some(Cursor(42)));
    }

    #[test]
    fn request_serializes_current_json_rpc_shape() {
        let request = RpcRequest::new(RequestId(7), "events.list", json!({"limit": 20}));

        let value = serde_json::to_value(request).expect("request should serialize");

        assert_eq!(
            value,
            json!({
                "jsonrpc": "2.0",
                "id": 7,
                "method": "events.list",
                "params": {"limit": 20}
            })
        );
    }

    #[test]
    fn events_list_response_parses_current_result() {
        let message = r#"
        {
          "jsonrpc": "2.0",
          "id": 2,
          "result": {
            "events": [{
              "id": "evt_123",
              "event_type": "zeta.user_message",
              "source": "user",
              "payload": {"message": "hello"},
              "idempotency_key": "message-123",
              "caused_by": null,
              "session_id": "default",
              "run_id": "run_123",
              "turn_id": null,
              "timestamp_ms": 1754438400000,
              "cursor": 42
            }],
            "next_cursor": 42
          }
        }
        "#;

        let message: IncomingMessage =
            serde_json::from_str(message).expect("response should parse");
        let IncomingMessage::Success(response) = message else {
            panic!("expected success response");
        };
        let result: EventsListResult =
            serde_json::from_value(response.result).expect("result should parse");

        assert_eq!(response.id, RequestId(2));
        assert_eq!(result.events.len(), 1);
        assert_eq!(result.events[0].event_type, "zeta.user_message");
        assert_eq!(result.next_cursor, Some(Cursor(42)));
    }

    #[test]
    fn initialize_result_parses_current_protocol_identity() {
        let result: InitializeResult = serde_json::from_value(json!({
            "server": "zeta",
            "protocol": "0.1"
        }))
        .expect("initialize result should parse");

        assert_eq!(result.server, "zeta");
        assert_eq!(result.protocol, "0.1");
    }

    #[test]
    fn session_list_response_parses_current_activity_shape() {
        let result: SessionsListResult = serde_json::from_value(json!({
            "sessions": [{
                "session_id": "session_123",
                "agent_id": "zeta.master",
                "status": "queued",
                "cancellation_requested": false,
                "active_run_id": null,
                "queued_turns": 1,
                "active_wait": null,
                "latest_run": null,
                "updated_at": "2026-08-07T12:00:00Z",
                "future_field": true
            }]
        }))
        .expect("session list should parse");

        assert_eq!(result.sessions.len(), 1);
        assert_eq!(result.sessions[0].session_id(), "session_123");
        assert_eq!(result.sessions[0].agent_id(), "zeta.master");
        assert_eq!(result.sessions[0].status(), "queued");
    }

    #[test]
    fn submit_result_identifies_the_durable_event_and_session() {
        let result: SubmitResult = serde_json::from_value(json!({
            "event_id": "evt_123",
            "queue_item_id": "queue_123",
            "agent_id": "zeta.master",
            "session_id": "session_123",
            "run_id": "run_123",
            "status": "queued"
        }))
        .expect("submit result should parse");

        assert_eq!(result.event_id, "evt_123");
        assert_eq!(result.session_id, "session_123");
        assert_eq!(result.status, "queued");
    }
}
