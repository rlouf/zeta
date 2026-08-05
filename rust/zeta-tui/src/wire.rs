use serde::{Deserialize, Serialize};
use serde_json::Value;

#[derive(Clone, Copy, Debug, Deserialize, PartialEq, Eq, Serialize)]
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

#[derive(Debug, Deserialize, PartialEq, Eq, Serialize)]
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

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::{
        Cursor, Event, EventId, EventsListResult, IncomingMessage, InitializeResult, RequestId,
        RpcRequest, RunId, SessionId,
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
}
