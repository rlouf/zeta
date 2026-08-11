//! Per-side wire-v0 session state machines (sans-IO).
//!
//! A session consumes parsed envelopes and clock readings and returns
//! [`Action`]s; it performs no IO and reads no clock. Callers supply
//! both a monotonic [`Instant`] (for timeouts) and a wall-clock
//! RFC 3339 string (to stamp outgoing envelopes), because a state
//! machine that invents time cannot be replayed against the golden
//! session vectors. Both machines enforce the invariants the spec
//! states: no traffic before the handshake, the ack window is a hard
//! bound, heartbeats gate liveness, and shutdown is orderly.
//!
//! [`Instant`]: std::time::Instant

use std::collections::BTreeSet;
use std::time::{Duration, Instant};

use serde_json::{Map, Number, Value};

use crate::canonical::canonical_json;
use crate::envelope::{
    Ack, Call, CallInfo, CallResult, Common, Envelope, ErrorEnvelope, ErrorInfo,
    EventEnvelope, EventTypeDecl, Heartbeat, Hello, HelloAck, OperationDecl, Shutdown,
    MAX_INLINE_PAYLOAD_BYTES, PROTOCOL_VERSION,
};
use crate::error::WireError;

/// What a session asks its caller to do next.
#[derive(Clone, Debug, PartialEq)]
pub enum Action {
    /// Write this envelope to the peer.
    Send(Envelope),
    /// Runtime side: hand this event to the application, which must
    /// call [`RuntimeSession::acknowledge`] once it is durable.
    DeliverEvent(EventEnvelope),
    /// Plugin side: execute this call and answer through
    /// [`PluginSession::complete_call`].
    HandleCall(Call),
    /// Runtime side: an outstanding call resolved.
    CallResolved(CallInfo),
    /// The peer reported an error envelope.
    PeerError(ErrorEnvelope),
    /// The peer violated the protocol; the session survives.
    ProtocolViolation { rule: String, detail: String },
    /// Runtime side: kill and respawn the child.
    Kill { reason: String },
    /// Plugin side: leave the process.
    Exit { reason: String },
}

fn error_envelope(id: String, wall: &str, code: &str, message: &str) -> Envelope {
    Envelope::Error(ErrorEnvelope {
        common: Common::new(id, wall),
        code: code.to_string(),
        message: message.to_string(),
        retryable: false,
    })
}

/// Settings for the parent side of one session.
#[derive(Clone, Debug)]
pub struct RuntimeConfig {
    /// Runtime identification for `hello_ack`, e.g. `zeta-os/0.1.0`.
    pub runtime_id: String,
    /// Non-secret settings for the child (`hello_ack.config`).
    pub config: Option<Map<String, Value>>,
    /// How long the child may take to say hello.
    pub handshake_timeout: Duration,
    /// Consecutive missed heartbeat intervals that mean death.
    pub heartbeat_miss_limit: u32,
}

impl Default for RuntimeConfig {
    fn default() -> Self {
        RuntimeConfig {
            runtime_id: "zeta-ipc/0".to_string(),
            config: None,
            handshake_timeout: Duration::from_secs(10),
            heartbeat_miss_limit: 3,
        }
    }
}

#[derive(Debug)]
enum RuntimeState {
    AwaitingHello { since: Instant },
    Established(Established),
    Dead,
}

#[derive(Debug)]
struct Established {
    heartbeat_secs: f64,
    ack_window: u64,
    operations: BTreeSet<String>,
    last_seen: Instant,
    unacked: BTreeSet<String>,
    pending_calls: BTreeSet<String>,
}

/// The parent-side session machine: supervises one plugin child.
///
/// # Examples
///
/// ```
/// use std::time::Instant;
/// use zeta_ipc::session::{Action, RuntimeConfig, RuntimeSession};
/// use zeta_ipc::Envelope;
///
/// let now = Instant::now();
/// let mut session = RuntimeSession::new(RuntimeConfig::default(), now);
/// let hello = Envelope::parse_str(concat!(
///     r#"{"event_types":[],"id":"m-c-1","kind":"hello","name":"demo","#,
///     r#""plugin_version":"0","protocol_versions":[0],"role":"source","#,
///     r#""ts":"2026-08-10T12:00:00Z","v":0}"#,
/// ))
/// .unwrap();
/// let actions = session.on_envelope(&hello, now, "2026-08-10T12:00:00Z");
/// let Action::Send(Envelope::HelloAck(_)) = &actions[0] else {
///     panic!("a valid hello earns a hello_ack");
/// };
/// ```
pub struct RuntimeSession {
    config: RuntimeConfig,
    state: RuntimeState,
    next_message: u64,
}

impl RuntimeSession {
    /// Creates a session waiting for the child's `hello`.
    pub fn new(config: RuntimeConfig, now: Instant) -> Self {
        RuntimeSession {
            config,
            state: RuntimeState::AwaitingHello { since: now },
            next_message: 0,
        }
    }

    /// Returns whether the handshake has completed.
    pub fn is_established(&self) -> bool {
        match &self.state {
            RuntimeState::AwaitingHello { .. } => false,
            RuntimeState::Established(_) => true,
            RuntimeState::Dead => false,
        }
    }

    fn message_id(&mut self) -> String {
        self.next_message += 1;
        format!("m-runtime-{}", self.next_message)
    }

    /// Feeds one envelope from the child.
    pub fn on_envelope(&mut self, envelope: &Envelope, now: Instant, wall: &str) -> Vec<Action> {
        match &self.state {
            RuntimeState::Dead => return Vec::new(),
            RuntimeState::AwaitingHello { since } => {
                let _ = since;
                return self.on_handshake_envelope(envelope, now, wall);
            }
            RuntimeState::Established(_) => {}
        }
        let expected = match envelope {
            Envelope::Event(_) => true,
            Envelope::Heartbeat(_) => true,
            Envelope::Error(_) => true,
            Envelope::CallResult(_) => true,
            Envelope::Hello(_) => false,
            Envelope::HelloAck(_) => false,
            Envelope::Ack(_) => false,
            Envelope::Shutdown(_) => false,
            Envelope::Call(_) => false,
        };
        if !expected {
            let kind = format!("{:?}", envelope.kind());
            let id = self.message_id();
            self.state = RuntimeState::Dead;
            return vec![
                Action::Send(error_envelope(
                    id,
                    wall,
                    "protocol",
                    &format!("unexpected kind {kind} after handshake"),
                )),
                Action::Kill {
                    reason: format!("child sent unexpected kind {kind}"),
                },
            ];
        }
        let RuntimeState::Established(established) = &mut self.state else {
            return Vec::new();
        };
        established.last_seen = now;
        match envelope {
            Envelope::Event(event) => on_child_event(established, event),
            Envelope::Heartbeat(_) => Vec::new(),
            Envelope::Error(error) => vec![Action::PeerError(error.clone())],
            Envelope::CallResult(result) => on_call_result(established, result),
            Envelope::Hello(_)
            | Envelope::HelloAck(_)
            | Envelope::Ack(_)
            | Envelope::Shutdown(_)
            | Envelope::Call(_) => Vec::new(),
        }
    }

    fn on_handshake_envelope(
        &mut self,
        envelope: &Envelope,
        now: Instant,
        wall: &str,
    ) -> Vec<Action> {
        let Envelope::Hello(hello) = envelope else {
            self.state = RuntimeState::Dead;
            return vec![
                Action::ProtocolViolation {
                    rule: "handshake".to_string(),
                    detail: "the child must speak first with hello".to_string(),
                },
                Action::Kill {
                    reason: "traffic before hello".to_string(),
                },
            ];
        };
        if hello.role != "source" {
            let id = self.message_id();
            self.state = RuntimeState::Dead;
            return vec![
                Action::Send(error_envelope(
                    id,
                    wall,
                    "protocol",
                    &format!("role {:?} is not available in this runtime", hello.role),
                )),
                Action::Kill {
                    reason: format!("unsupported role {:?}", hello.role),
                },
            ];
        }
        if !hello.protocol_versions.contains(&PROTOCOL_VERSION) {
            let id = self.message_id();
            self.state = RuntimeState::Dead;
            return vec![
                Action::Send(error_envelope(
                    id,
                    wall,
                    "unsupported_version",
                    &format!("runtime speaks protocol {PROTOCOL_VERSION} only"),
                )),
                Action::Kill {
                    reason: "no common protocol version".to_string(),
                },
            ];
        }
        let heartbeat_secs = match &hello.heartbeat_secs {
            Some(number) => number.as_f64().unwrap_or(10.0),
            None => 10.0,
        };
        let ack_window = hello.ack_window.unwrap_or(64);
        let mut operations = BTreeSet::new();
        if let Some(declared) = &hello.operations {
            for OperationDecl { name, extra } in declared {
                let _ = extra;
                operations.insert(name.clone());
            }
        }
        self.state = RuntimeState::Established(Established {
            heartbeat_secs,
            ack_window,
            operations,
            last_seen: now,
            unacked: BTreeSet::new(),
            pending_calls: BTreeSet::new(),
        });
        let mut ack = HelloAck {
            common: Common::new(self.message_id(), wall),
            protocol_version: PROTOCOL_VERSION,
            runtime: self.config.runtime_id.clone(),
            config: None,
        };
        ack.config = self.config.config.clone();
        vec![Action::Send(Envelope::HelloAck(ack))]
    }

    /// Marks one delivered event as durably accepted and acks it.
    ///
    /// Acks are application-driven because an ack means "journaled",
    /// not "received"; the machine only bookkeeps the window.
    pub fn acknowledge(&mut self, event_id: &str, wall: &str) -> Vec<Action> {
        let RuntimeState::Established(established) = &mut self.state else {
            return Vec::new();
        };
        if !established.unacked.remove(event_id) {
            return Vec::new();
        }
        let ack = Ack {
            common: Common::new(self.message_id(), wall),
            event_id: event_id.to_string(),
        };
        vec![Action::Send(Envelope::Ack(ack))]
    }

    /// Invokes one operation the child declared in its hello.
    ///
    /// # Errors
    ///
    /// Returns [`WireError`] before the handshake or for an
    /// undeclared operation.
    pub fn send_call(
        &mut self,
        name: &str,
        payload: Map<String, Value>,
        effect_key: &str,
        wall: &str,
    ) -> Result<Vec<Action>, WireError> {
        let call_id = self.message_id();
        let RuntimeState::Established(established) = &mut self.state else {
            return Err(WireError::new(
                "protocol",
                "calls are only valid after the handshake",
            ));
        };
        if !established.operations.contains(name) {
            return Err(WireError::new(
                "protocol",
                format!("operation {name:?} was not declared by the child"),
            ));
        }
        established.pending_calls.insert(call_id.clone());
        let call = Call {
            common: Common::new(call_id, wall),
            name: name.to_string(),
            payload,
            effect_key: effect_key.to_string(),
        };
        Ok(vec![Action::Send(Envelope::Call(call))])
    }

    /// Requests an orderly stop; escalation timing is the caller's.
    pub fn shutdown(&mut self, reason: &str, wall: &str) -> Vec<Action> {
        let id = self.message_id();
        self.state = RuntimeState::Dead;
        let shutdown = Shutdown {
            common: Common::new(id, wall),
            reason: Some(reason.to_string()),
        };
        vec![Action::Send(Envelope::Shutdown(shutdown))]
    }

    /// Advances the clock: handshake and heartbeat deadlines.
    pub fn on_tick(&mut self, now: Instant) -> Vec<Action> {
        match &self.state {
            RuntimeState::Dead => Vec::new(),
            RuntimeState::AwaitingHello { since } => {
                if now.duration_since(*since) > self.config.handshake_timeout {
                    self.state = RuntimeState::Dead;
                    return vec![Action::Kill {
                        reason: "no hello within the handshake timeout".to_string(),
                    }];
                }
                Vec::new()
            }
            RuntimeState::Established(established) => {
                let limit = established.heartbeat_secs
                    * f64::from(self.config.heartbeat_miss_limit);
                let silent = now.duration_since(established.last_seen).as_secs_f64();
                if silent > limit {
                    self.state = RuntimeState::Dead;
                    return vec![Action::Kill {
                        reason: format!(
                            "no heartbeat for {silent:.0}s ({} missed intervals)",
                            self.config.heartbeat_miss_limit
                        ),
                    }];
                }
                Vec::new()
            }
        }
    }
}

fn on_child_event(established: &mut Established, event: &EventEnvelope) -> Vec<Action> {
    established.unacked.insert(event.common.id.clone());
    let outstanding = established.unacked.len() as u64;
    if outstanding > established.ack_window {
        return vec![Action::Kill {
            reason: format!(
                "ack window exceeded: {outstanding} unacked events for a window of {}",
                established.ack_window
            ),
        }];
    }
    vec![Action::DeliverEvent(event.clone())]
}

fn on_call_result(established: &mut Established, result: &CallResult) -> Vec<Action> {
    if !established.pending_calls.remove(&result.call_id) {
        return vec![Action::ProtocolViolation {
            rule: "protocol".to_string(),
            detail: format!("stray call_result {:?}", result.call_id),
        }];
    }
    let outcome = match (&result.result, &result.error) {
        (Some(payload), _) if result.ok => Ok(payload.clone()),
        (_, Some(error)) => Err(error.clone()),
        (_, None) => Err(ErrorInfo {
            code: "internal".to_string(),
            message: "call_result carried no error detail".to_string(),
            retryable: false,
            extra: Map::new(),
        }),
    };
    vec![Action::CallResolved(CallInfo {
        call_id: result.call_id.clone(),
        outcome,
    })]
}

/// Settings for the child side of one session.
#[derive(Clone, Debug)]
pub struct PluginConfig {
    pub name: String,
    pub plugin_version: String,
    pub event_types: Vec<EventTypeDecl>,
    pub operations: Vec<String>,
    pub capabilities: Option<Map<String, Value>>,
    pub heartbeat_secs: Number,
    pub ack_window: u64,
}

impl PluginConfig {
    /// Creates a source config with the spec defaults.
    pub fn source(name: &str, plugin_version: &str, event_types: Vec<EventTypeDecl>) -> Self {
        PluginConfig {
            name: name.to_string(),
            plugin_version: plugin_version.to_string(),
            event_types,
            operations: Vec::new(),
            capabilities: None,
            heartbeat_secs: Number::from(10u64),
            ack_window: 64,
        }
    }
}

enum PluginState {
    Idle,
    AwaitingHelloAck,
    Established(PluginEstablished),
    Exited,
}

struct PluginEstablished {
    config: Map<String, Value>,
    unacked: BTreeSet<String>,
    open_calls: BTreeSet<String>,
    last_beat: Instant,
}

/// The child-side session machine: one plugin speaking to a runtime.
pub struct PluginSession {
    config: PluginConfig,
    state: PluginState,
    next_message: u64,
}

impl PluginSession {
    /// Creates a session that has not yet said hello.
    pub fn new(config: PluginConfig) -> Self {
        PluginSession {
            config,
            state: PluginState::Idle,
            next_message: 0,
        }
    }

    fn message_id(&mut self) -> String {
        self.next_message += 1;
        format!("m-{}-{}", self.config.name, self.next_message)
    }

    /// Returns the runtime-supplied settings after the handshake.
    pub fn runtime_config(&self) -> Option<&Map<String, Value>> {
        match &self.state {
            PluginState::Idle => None,
            PluginState::AwaitingHelloAck => None,
            PluginState::Established(established) => Some(&established.config),
            PluginState::Exited => None,
        }
    }

    /// Opens the handshake: the child speaks first.
    pub fn start(&mut self, now: Instant, wall: &str) -> Vec<Action> {
        let PluginState::Idle = self.state else {
            return Vec::new();
        };
        let _ = now;
        let mut operations = Vec::new();
        for name in &self.config.operations {
            operations.push(OperationDecl {
                name: name.clone(),
                extra: Map::new(),
            });
        }
        let capabilities = match &self.config.capabilities {
            Some(capabilities) => capabilities.clone(),
            None => {
                let mut defaults = Map::new();
                defaults.insert("effects_are_proposals".to_string(), Value::Bool(false));
                defaults
            }
        };
        let hello = Hello {
            common: Common::new(self.message_id(), wall),
            name: self.config.name.clone(),
            plugin_version: self.config.plugin_version.clone(),
            role: "source".to_string(),
            protocol_versions: vec![PROTOCOL_VERSION],
            event_types: Some(self.config.event_types.clone()),
            operations: Some(operations),
            capabilities: Some(capabilities),
            heartbeat_secs: Some(self.config.heartbeat_secs.clone()),
            ack_window: Some(self.config.ack_window),
        };
        self.state = PluginState::AwaitingHelloAck;
        vec![Action::Send(Envelope::Hello(hello))]
    }

    /// Feeds one envelope from the runtime.
    pub fn on_envelope(&mut self, envelope: &Envelope, now: Instant) -> Vec<Action> {
        match &mut self.state {
            PluginState::Idle => vec![Action::ProtocolViolation {
                rule: "handshake".to_string(),
                detail: "the session has not said hello yet".to_string(),
            }],
            PluginState::Exited => Vec::new(),
            PluginState::AwaitingHelloAck => {
                let Envelope::HelloAck(ack) = envelope else {
                    self.state = PluginState::Exited;
                    return vec![Action::Exit {
                        reason: "expected hello_ack first".to_string(),
                    }];
                };
                if ack.protocol_version != PROTOCOL_VERSION {
                    self.state = PluginState::Exited;
                    return vec![Action::Exit {
                        reason: format!(
                            "unsupported protocol version {}",
                            ack.protocol_version
                        ),
                    }];
                }
                let config = match &ack.config {
                    Some(config) => config.clone(),
                    None => Map::new(),
                };
                self.state = PluginState::Established(PluginEstablished {
                    config,
                    unacked: BTreeSet::new(),
                    open_calls: BTreeSet::new(),
                    last_beat: now,
                });
                Vec::new()
            }
            PluginState::Established(established) => match envelope {
                Envelope::Ack(ack) => {
                    established.unacked.remove(&ack.event_id);
                    Vec::new()
                }
                Envelope::Call(call) => {
                    established.open_calls.insert(call.common.id.clone());
                    vec![Action::HandleCall(call.clone())]
                }
                Envelope::Shutdown(shutdown) => {
                    let reason = match &shutdown.reason {
                        Some(reason) => reason.clone(),
                        None => "shutdown requested".to_string(),
                    };
                    self.state = PluginState::Exited;
                    vec![Action::Exit { reason }]
                }
                Envelope::Error(error) => vec![Action::PeerError(error.clone())],
                Envelope::Hello(_)
                | Envelope::HelloAck(_)
                | Envelope::Event(_)
                | Envelope::Heartbeat(_)
                | Envelope::CallResult(_) => vec![Action::ProtocolViolation {
                    rule: "protocol".to_string(),
                    detail: format!("unexpected parent kind {:?}", envelope.kind()),
                }],
            },
        }
    }

    /// Emits one event, minting its deterministic id (spec §6.1).
    ///
    /// # Errors
    ///
    /// Returns [`WireError`] before the handshake, when the ack
    /// window is full (the caller waits for acks), for an undeclared
    /// event type, or for an oversized inline payload.
    pub fn send_event(
        &mut self,
        event_type: &str,
        payload: Map<String, Value>,
        caused_by: Option<String>,
        session_id: Option<String>,
        wall: &str,
    ) -> Result<Vec<Action>, WireError> {
        let mut schema = None;
        for declared in &self.config.event_types {
            if declared.event_type == event_type {
                schema = Some(declared.schema.clone());
                break;
            }
        }
        let Some(schema) = schema else {
            return Err(WireError::new(
                "protocol",
                format!("event type {event_type:?} was not declared in the hello"),
            ));
        };
        let window = self.config.ack_window;
        let PluginState::Established(established) = &mut self.state else {
            return Err(WireError::new(
                "protocol",
                "events are only valid after the handshake",
            ));
        };
        if established.unacked.len() as u64 >= window {
            return Err(WireError::new(
                "protocol",
                format!("the ack window of {window} is full; wait for acks"),
            ));
        }
        let mut identity = Map::new();
        identity.insert("payload".to_string(), Value::Object(payload.clone()));
        identity.insert("type".to_string(), Value::String(event_type.to_string()));
        let identity = canonical_json(&Value::Object(identity));
        if identity.len() > MAX_INLINE_PAYLOAD_BYTES {
            return Err(WireError::new(
                "payload_too_large",
                "inline payloads are limited to 64 KiB",
            ));
        }
        let event_id = zeta_substrate::derive(zeta_substrate::Domain::Event, identity.as_bytes());
        let event_id = event_id.to_string();
        established.unacked.insert(event_id.clone());
        let event = EventEnvelope {
            common: Common::new(event_id, wall),
            event_type: event_type.to_string(),
            schema,
            caused_by,
            session_id,
            payload: Some(payload),
            payload_hash: None,
        };
        Ok(vec![Action::Send(Envelope::Event(event))])
    }

    /// Answers one open call exactly once.
    ///
    /// # Errors
    ///
    /// Returns [`WireError`] for a call id that is not open.
    pub fn complete_call(
        &mut self,
        call_id: &str,
        outcome: Result<Map<String, Value>, ErrorInfo>,
        wall: &str,
    ) -> Result<Vec<Action>, WireError> {
        let id = self.message_id();
        let PluginState::Established(established) = &mut self.state else {
            return Err(WireError::new(
                "protocol",
                "call results are only valid after the handshake",
            ));
        };
        if !established.open_calls.remove(call_id) {
            return Err(WireError::new(
                "protocol",
                format!("call {call_id:?} is not open"),
            ));
        }
        let result = match outcome {
            Ok(payload) => CallResult {
                common: Common::new(id, wall),
                call_id: call_id.to_string(),
                ok: true,
                result: Some(payload),
                error: None,
            },
            Err(error) => CallResult {
                common: Common::new(id, wall),
                call_id: call_id.to_string(),
                ok: false,
                result: None,
                error: Some(error),
            },
        };
        Ok(vec![Action::Send(Envelope::CallResult(result))])
    }

    /// Advances the clock: emits a heartbeat when one is due.
    pub fn on_tick(&mut self, now: Instant, wall: &str) -> Vec<Action> {
        let interval = self.config.heartbeat_secs.as_f64().unwrap_or(10.0);
        let due = match &self.state {
            PluginState::Idle => false,
            PluginState::AwaitingHelloAck => false,
            PluginState::Exited => false,
            PluginState::Established(established) => {
                now.duration_since(established.last_beat).as_secs_f64() >= interval
            }
        };
        if !due {
            return Vec::new();
        }
        let id = self.message_id();
        let PluginState::Established(established) = &mut self.state else {
            return Vec::new();
        };
        established.last_beat = now;
        let heartbeat = Heartbeat {
            common: Common::new(id, wall),
        };
        vec![Action::Send(Envelope::Heartbeat(heartbeat))]
    }
}
