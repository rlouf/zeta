//! Bidirectional, sans-IO IPC session state.
//!
//! A session owns initialization, role gates, request correlation, flow
//! limits, and protocol liveness. Callers own byte IO, timers, application
//! method handlers, and process supervision.

use std::collections::HashMap;

use serde_json::{Map, Value};

use crate::error::{
    ErrorObject, IpcError, Retryability, INTERNAL_ERROR, INVALID_PARAMS, INVALID_REQUEST,
    METHOD_NOT_FOUND, SERVER_ERROR,
};
use crate::message::{
    ErrorResponse, InitializeParams, InitializeResult, Message, Notification, PeerIdentity,
    Request, RequestId, Role, SuccessResponse, PROTOCOL_VERSION,
};
use crate::validate::{
    method_is_reserved, parse_initialize_params, validate_direct_request,
    validate_event_notification, validate_fixed_request, validate_initialize_params,
    validate_initialize_result, validate_result,
};

/// States which process may request orderly shutdown on this connection.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ShutdownDirection {
    /// Permits this session to request shutdown of the remote process.
    LocalSupervisesRemote,
    /// Permits the remote process to request shutdown of this process.
    RemoteSupervisesLocal,
    /// Permits neither process to request shutdown.
    Disabled,
}

/// Configures the runtime end of an IPC session.
#[derive(Clone, Debug, PartialEq)]
pub struct RuntimeConfig {
    /// Identifies the runtime in the initialization result.
    pub runtime: PeerIdentity,
    /// Contains non-secret settings for the peer.
    pub config: Map<String, Value>,
    /// Lists the roles this runtime accepts.
    pub supported_roles: Vec<Role>,
    /// Contains the default heartbeat interval.
    pub heartbeat_seconds: f64,
    /// Contains the maximum unanswered-request limit.
    pub max_in_flight: u64,
}

impl RuntimeConfig {
    /// Creates a runtime configuration with protocol defaults.
    pub fn new(runtime: PeerIdentity) -> Self {
        Self {
            runtime,
            config: Map::new(),
            supported_roles: vec![Role::Source, Role::Client, Role::Provider],
            heartbeat_seconds: 10.0,
            max_in_flight: 64,
        }
    }
}

/// Contains the outcome of one locally initiated request.
#[derive(Clone, Debug, PartialEq)]
pub struct ResolvedRequest {
    /// Identifies the resolved request.
    pub id: RequestId,
    /// Names the method that resolved.
    pub method: String,
    /// Contains either the result or the peer's error object.
    pub outcome: Result<Value, ErrorObject>,
}

/// Describes work that a session asks its caller to perform.
#[derive(Clone, Debug, PartialEq)]
pub enum Action {
    /// Sends one message to the remote process.
    Send(Message),
    /// Invokes an application request handler.
    HandleRequest(Request),
    /// Delivers an application notification.
    HandleNotification(Notification),
    /// Delivers the outcome of a locally initiated request.
    RequestResolved(ResolvedRequest),
    /// Reports a protocol violation without choosing process policy.
    Violation(IpcError),
    /// Closes the local side after queued sends are flushed.
    Close {
        /// Contains the requested or detected close reason.
        reason: Option<String>,
    },
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Side {
    Peer,
    Runtime,
}

#[derive(Clone, Debug, PartialEq, Eq)]
enum State {
    PeerIdle,
    PeerAwaitingInitialization,
    RuntimeAwaitingInitialization,
    Initialized,
    Closed,
}

/// Tracks one bidirectional JSON-RPC connection.
pub struct Session {
    side: Side,
    state: State,
    peer_params: Option<InitializeParams>,
    runtime_config: Option<RuntimeConfig>,
    initialize_result: Option<InitializeResult>,
    shutdown_direction: ShutdownDirection,
    incoming: HashMap<RequestId, String>,
    outgoing: HashMap<RequestId, String>,
    max_in_flight: u64,
    activity_since_tick: bool,
    missed_intervals: u8,
}

impl Session {
    /// Creates the peer end of an uninitialized session.
    pub fn peer(params: InitializeParams, shutdown_direction: ShutdownDirection) -> Self {
        let max_in_flight = params.max_in_flight.unwrap_or(64);
        Self {
            side: Side::Peer,
            state: State::PeerIdle,
            peer_params: Some(params),
            runtime_config: None,
            initialize_result: None,
            shutdown_direction,
            incoming: HashMap::new(),
            outgoing: HashMap::new(),
            max_in_flight,
            activity_since_tick: false,
            missed_intervals: 0,
        }
    }

    /// Creates the runtime end of a session that awaits initialization.
    pub fn runtime(config: RuntimeConfig, shutdown_direction: ShutdownDirection) -> Self {
        let max_in_flight = config.max_in_flight;
        Self {
            side: Side::Runtime,
            state: State::RuntimeAwaitingInitialization,
            peer_params: None,
            runtime_config: Some(config),
            initialize_result: None,
            shutdown_direction,
            incoming: HashMap::new(),
            outgoing: HashMap::new(),
            max_in_flight,
            activity_since_tick: false,
            missed_intervals: 0,
        }
    }

    /// Returns whether initialization completed successfully.
    pub fn is_initialized(&self) -> bool {
        self.state == State::Initialized
    }

    /// Returns the peer's accepted initialization parameters.
    pub fn peer_parameters(&self) -> Option<&InitializeParams> {
        if self.is_initialized() {
            self.peer_params.as_ref()
        } else {
            None
        }
    }

    /// Returns the runtime's negotiated initialization result.
    pub fn initialization_result(&self) -> Option<&InitializeResult> {
        if self.is_initialized() {
            self.initialize_result.as_ref()
        } else {
            None
        }
    }

    /// Returns the number of unanswered remote requests.
    pub fn incoming_request_count(&self) -> usize {
        self.incoming.len()
    }

    /// Returns the number of unanswered local requests.
    pub fn outgoing_request_count(&self) -> usize {
        self.outgoing.len()
    }

    /// Starts peer-to-runtime initialization.
    ///
    /// # Errors
    ///
    /// Returns [`IpcError`] for invalid initialization parameters, a duplicate
    /// request id, or a session in the wrong state.
    pub fn initialize(&mut self, id: RequestId) -> Result<Vec<Action>, IpcError> {
        if self.side != Side::Peer || self.state != State::PeerIdle {
            return Err(IpcError::new(
                INVALID_REQUEST,
                "the session cannot initialize in its current state",
            ));
        }
        let Some(params) = &self.peer_params else {
            return Err(IpcError::new(
                INTERNAL_ERROR,
                "the peer session has no initialization parameters",
            ));
        };
        validate_initialize_params(params)?;
        if self.outgoing.contains_key(&id) {
            return Err(IpcError::new(
                INVALID_REQUEST,
                format!("request id {id:?} is already pending"),
            ));
        }
        let request = Request::new(id.clone(), "initialize", params.to_map());
        self.outgoing.insert(id, "initialize".to_string());
        self.state = State::PeerAwaitingInitialization;
        Ok(vec![Action::Send(Message::Request(request))])
    }

    /// Sends one role-authorized request after initialization.
    ///
    /// # Errors
    ///
    /// Returns [`IpcError`] for invalid parameters, insufficient role or
    /// process authority, a duplicate id, or a full flow-control window.
    pub fn send_request(
        &mut self,
        id: RequestId,
        method: impl Into<String>,
        params: Map<String, Value>,
    ) -> Result<Vec<Action>, IpcError> {
        if self.state != State::Initialized {
            return Err(IpcError::new(
                INVALID_REQUEST,
                "requests require an initialized session",
            ));
        }
        let method = method.into();
        self.validate_outgoing_request(&method, &params)?;
        if self.outgoing.contains_key(&id) {
            return Err(IpcError::new(
                INVALID_REQUEST,
                format!("request id {id:?} is already pending"),
            ));
        }
        if self.outgoing.len() as u64 >= self.max_in_flight {
            return Err(IpcError::new(
                SERVER_ERROR,
                format!(
                    "the in-flight request limit of {} is full",
                    self.max_in_flight
                ),
            ));
        }
        let request = Request::new(id.clone(), method.clone(), params);
        self.outgoing.insert(id, method);
        Ok(vec![Action::Send(Message::Request(request))])
    }

    /// Sends one role-authorized notification.
    ///
    /// # Errors
    ///
    /// Returns [`IpcError`] unless the runtime sends `event` to a client peer
    /// with a valid durable-event value.
    pub fn send_notification(
        &self,
        method: impl Into<String>,
        params: Map<String, Value>,
    ) -> Result<Vec<Action>, IpcError> {
        if self.state != State::Initialized {
            return Err(IpcError::new(
                INVALID_REQUEST,
                "notifications require an initialized session",
            ));
        }
        let method = method.into();
        if self.side != Side::Runtime || method != "event" || !self.has_peer_role(Role::Client) {
            return Err(IpcError::new(
                METHOD_NOT_FOUND,
                format!("notification method {method:?} is not available"),
            ));
        }
        validate_event_notification(&params)?;
        Ok(vec![Action::Send(Message::Notification(
            Notification::new(method, params),
        ))])
    }

    /// Consumes one parsed message and returns the resulting actions.
    pub fn receive(&mut self, message: Message) -> Vec<Action> {
        if self.state == State::Closed {
            return Vec::new();
        }
        self.activity_since_tick = true;
        self.missed_intervals = 0;
        match message {
            Message::Request(request) => self.receive_request(request),
            Message::Notification(notification) => self.receive_notification(notification),
            Message::Success(response) => self.receive_success(response),
            Message::Error(response) => self.receive_error(response),
        }
    }

    /// Completes one incoming application request successfully.
    ///
    /// # Errors
    ///
    /// Returns [`IpcError`] for a request id that is not pending or a result
    /// that violates the method's protocol shape.
    pub fn complete_request(
        &mut self,
        id: &RequestId,
        result: Value,
    ) -> Result<Vec<Action>, IpcError> {
        let Some(method) = self.incoming.get(id) else {
            return Err(IpcError::new(
                INVALID_REQUEST,
                format!("request id {id:?} is not pending"),
            ));
        };
        validate_result(method, &result)?;
        self.incoming.remove(id);
        Ok(vec![Action::Send(Message::Success(SuccessResponse::new(
            id.clone(),
            result,
        )))])
    }

    /// Completes one incoming application request with an error.
    ///
    /// # Errors
    ///
    /// Returns [`IpcError`] for a request id that is not pending.
    pub fn fail_request(
        &mut self,
        id: &RequestId,
        error: ErrorObject,
    ) -> Result<Vec<Action>, IpcError> {
        if self.incoming.remove(id).is_none() {
            return Err(IpcError::new(
                INVALID_REQUEST,
                format!("request id {id:?} is not pending"),
            ));
        }
        Ok(vec![Action::Send(Message::Error(ErrorResponse::new(
            Some(id.clone()),
            error,
        )))])
    }

    /// Advances one negotiated heartbeat interval.
    ///
    /// A supervising side emits `ping` after one idle interval and requests
    /// closure after three consecutive idle intervals. Any parsed incoming
    /// message resets the counter.
    pub fn on_tick(&mut self, ping_id: RequestId) -> Vec<Action> {
        if self.state != State::Initialized
            || self.shutdown_direction != ShutdownDirection::LocalSupervisesRemote
        {
            return Vec::new();
        }
        if self.activity_since_tick {
            self.activity_since_tick = false;
            self.missed_intervals = 0;
            return Vec::new();
        }
        self.missed_intervals = self.missed_intervals.saturating_add(1);
        if self.missed_intervals >= 3 {
            self.state = State::Closed;
            return vec![Action::Close {
                reason: Some("the peer missed three heartbeat intervals".to_string()),
            }];
        }
        match self.send_request(ping_id, "ping", Map::new()) {
            Ok(actions) => actions,
            Err(error) => vec![Action::Violation(error)],
        }
    }

    fn receive_request(&mut self, request: Request) -> Vec<Action> {
        if request.method == "initialize" {
            if self.side == Side::Runtime && self.state == State::RuntimeAwaitingInitialization {
                return self.receive_initialize(request);
            }
            return self.reject_request_with_code(
                request,
                IpcError::new(INVALID_REQUEST, "a connection may initialize only once"),
                "already_initialized",
            );
        }
        if self.state != State::Initialized {
            let actions = self.reject_request_with_code(
                request,
                IpcError::new(INVALID_REQUEST, "initialize must be the first request"),
                "not_initialized",
            );
            self.state = State::Closed;
            return with_close(actions, "traffic before initialization");
        }
        if self.incoming.contains_key(&request.id) {
            return self.reject_request(
                request,
                IpcError::new(INVALID_REQUEST, "the request id is already pending"),
            );
        }
        if request.method == "ping" {
            let validation = validate_fixed_request(&request.method, &request.params);
            if let Err(error) = validation {
                return self.reject_request(request, error);
            }
            return vec![Action::Send(Message::Success(SuccessResponse::new(
                request.id,
                Value::Object(Map::new()),
            )))];
        }
        if request.method == "shutdown" {
            return self.receive_shutdown(request);
        }
        let validation = self.validate_incoming_request(&request.method, &request.params);
        if let Err(error) = validation {
            return self.reject_request(request, error);
        }
        if self.incoming.len() as u64 >= self.max_in_flight {
            return self.reject_request(
                request,
                IpcError::new(
                    SERVER_ERROR,
                    format!(
                        "the in-flight request limit of {} is full",
                        self.max_in_flight
                    ),
                ),
            );
        }
        self.incoming
            .insert(request.id.clone(), request.method.clone());
        vec![Action::HandleRequest(request)]
    }

    fn receive_initialize(&mut self, request: Request) -> Vec<Action> {
        let params = parse_initialize_params(&request.params);
        let Ok(params) = params else {
            let error = params.expect_err("the let-else observed an error");
            let actions = self.reject_request(request, error);
            self.state = State::Closed;
            return with_close(actions, "initialization failed");
        };
        if !params.protocol_versions.contains(&PROTOCOL_VERSION) {
            let error = ErrorObject::application(
                SERVER_ERROR,
                "unsupported_version",
                "No supported IPC protocol version",
                Retryability::Final,
            );
            self.state = State::Closed;
            return vec![
                Action::Send(Message::Error(ErrorResponse::new(Some(request.id), error))),
                Action::Close {
                    reason: Some("initialization failed".to_string()),
                },
            ];
        }
        let Some(config) = self.runtime_config.clone() else {
            self.state = State::Closed;
            return self.reject_request(
                request,
                IpcError::new(INTERNAL_ERROR, "the runtime configuration is unavailable"),
            );
        };
        for role in &params.roles {
            if !config.supported_roles.contains(role) {
                let error = IpcError::new(
                    INVALID_PARAMS,
                    format!("role {role:?} is not supported by this runtime"),
                );
                let actions = self.reject_request(request, error);
                self.state = State::Closed;
                return with_close(actions, "initialization failed");
            }
        }
        let heartbeat_seconds = params.heartbeat_seconds.unwrap_or(config.heartbeat_seconds);
        let requested_limit = params.max_in_flight.unwrap_or(config.max_in_flight);
        let max_in_flight = requested_limit.min(config.max_in_flight);
        let result = InitializeResult {
            protocol_version: PROTOCOL_VERSION,
            runtime: config.runtime,
            roles: params.roles.clone(),
            config: config.config,
            heartbeat_seconds,
            max_in_flight,
        };
        self.peer_params = Some(params);
        self.initialize_result = Some(result.clone());
        self.max_in_flight = max_in_flight;
        self.state = State::Initialized;
        self.activity_since_tick = false;
        vec![Action::Send(Message::Success(SuccessResponse::new(
            request.id,
            result.to_value(),
        )))]
    }

    fn receive_shutdown(&mut self, request: Request) -> Vec<Action> {
        if self.shutdown_direction != ShutdownDirection::RemoteSupervisesLocal {
            return self.reject_request(
                request,
                IpcError::new(METHOD_NOT_FOUND, "shutdown is not authorized"),
            );
        }
        let validation = validate_fixed_request(&request.method, &request.params);
        if let Err(error) = validation {
            return self.reject_request(request, error);
        }
        let reason = request
            .params
            .get("reason")
            .and_then(Value::as_str)
            .map(str::to_string);
        self.state = State::Closed;
        vec![
            Action::Send(Message::Success(SuccessResponse::new(
                request.id,
                Value::Object(Map::new()),
            ))),
            Action::Close { reason },
        ]
    }

    fn receive_notification(&mut self, notification: Notification) -> Vec<Action> {
        if self.state != State::Initialized {
            self.state = State::Closed;
            return vec![
                Action::Violation(IpcError::new(
                    INVALID_REQUEST,
                    "initialize must complete before notifications",
                )),
                Action::Close {
                    reason: Some("traffic before initialization".to_string()),
                },
            ];
        }
        if self.side == Side::Peer
            && self.has_peer_role(Role::Client)
            && notification.method == "event"
        {
            let validation = validate_event_notification(&notification.params);
            return match validation {
                Ok(()) => vec![Action::HandleNotification(notification)],
                Err(error) => vec![Action::Violation(error)],
            };
        }
        vec![Action::Violation(IpcError::new(
            METHOD_NOT_FOUND,
            format!(
                "notification method {:?} is not available",
                notification.method
            ),
        ))]
    }

    fn receive_success(&mut self, response: SuccessResponse) -> Vec<Action> {
        let Some(method) = self.outgoing.remove(&response.id) else {
            return vec![Action::Violation(IpcError::new(
                INVALID_REQUEST,
                format!("response id {:?} is not pending", response.id),
            ))];
        };
        if method == "initialize" {
            return self.receive_initialize_success(response, method);
        }
        if let Err(error) = validate_result(&method, &response.result) {
            return vec![Action::Violation(error)];
        }
        vec![Action::RequestResolved(ResolvedRequest {
            id: response.id,
            method,
            outcome: Ok(response.result),
        })]
    }

    fn receive_initialize_success(
        &mut self,
        response: SuccessResponse,
        method: String,
    ) -> Vec<Action> {
        if self.side != Side::Peer || self.state != State::PeerAwaitingInitialization {
            self.state = State::Closed;
            return vec![
                Action::Violation(IpcError::new(
                    INVALID_REQUEST,
                    "an initialization response arrived in the wrong state",
                )),
                Action::Close {
                    reason: Some("initialization failed".to_string()),
                },
            ];
        }
        let result = InitializeResult::from_value(&response.result);
        let Ok(result) = result else {
            let error = result.expect_err("the let-else observed an error");
            self.state = State::Closed;
            return vec![
                Action::Violation(error),
                Action::Close {
                    reason: Some("initialization failed".to_string()),
                },
            ];
        };
        let Some(params) = &self.peer_params else {
            self.state = State::Closed;
            return vec![
                Action::Violation(IpcError::new(
                    INTERNAL_ERROR,
                    "the peer initialization parameters are unavailable",
                )),
                Action::Close {
                    reason: Some("initialization failed".to_string()),
                },
            ];
        };
        if let Err(error) = validate_initialize_result(&result, &params.roles) {
            self.state = State::Closed;
            return vec![
                Action::Violation(error),
                Action::Close {
                    reason: Some("initialization failed".to_string()),
                },
            ];
        }
        self.max_in_flight = result.max_in_flight;
        self.initialize_result = Some(result);
        self.state = State::Initialized;
        self.activity_since_tick = false;
        vec![Action::RequestResolved(ResolvedRequest {
            id: response.id,
            method,
            outcome: Ok(response.result),
        })]
    }

    fn receive_error(&mut self, response: ErrorResponse) -> Vec<Action> {
        let Some(id) = response.id else {
            return vec![Action::Violation(IpcError::new(
                INVALID_REQUEST,
                "an error response with null id cannot resolve a request",
            ))];
        };
        let Some(method) = self.outgoing.remove(&id) else {
            return vec![Action::Violation(IpcError::new(
                INVALID_REQUEST,
                format!("response id {id:?} is not pending"),
            ))];
        };
        let initializing = method == "initialize";
        let mut actions = vec![Action::RequestResolved(ResolvedRequest {
            id,
            method,
            outcome: Err(response.error),
        })];
        if initializing {
            self.state = State::Closed;
            actions.push(Action::Close {
                reason: Some("initialization failed".to_string()),
            });
        }
        actions
    }

    fn validate_outgoing_request(
        &self,
        method: &str,
        params: &Map<String, Value>,
    ) -> Result<(), IpcError> {
        if method == "ping" {
            return validate_fixed_request(method, params);
        }
        if method == "shutdown" {
            if self.shutdown_direction != ShutdownDirection::LocalSupervisesRemote {
                return Err(IpcError::new(
                    METHOD_NOT_FOUND,
                    "shutdown is not authorized",
                ));
            }
            return validate_fixed_request(method, params);
        }
        match self.side {
            Side::Peer => self.validate_peer_outgoing(method, params),
            Side::Runtime => self.validate_runtime_outgoing(method, params),
        }
    }

    fn validate_peer_outgoing(
        &self,
        method: &str,
        params: &Map<String, Value>,
    ) -> Result<(), IpcError> {
        if method == "events.publish" {
            if !self.has_peer_role(Role::Source) {
                return method_not_found(method);
            }
            validate_fixed_request(method, params)?;
            return self.validate_published_type(params);
        }
        if is_client_method(method) {
            if !self.has_peer_role(Role::Client) {
                return method_not_found(method);
            }
            return validate_fixed_request(method, params);
        }
        method_not_found(method)
    }

    fn validate_runtime_outgoing(
        &self,
        method: &str,
        params: &Map<String, Value>,
    ) -> Result<(), IpcError> {
        if method_is_reserved(method) || !self.peer_declares_method(method) {
            return method_not_found(method);
        }
        validate_direct_request(params)
    }

    fn validate_incoming_request(
        &self,
        method: &str,
        params: &Map<String, Value>,
    ) -> Result<(), IpcError> {
        match self.side {
            Side::Runtime => self.validate_runtime_incoming(method, params),
            Side::Peer => self.validate_peer_incoming(method, params),
        }
    }

    fn validate_runtime_incoming(
        &self,
        method: &str,
        params: &Map<String, Value>,
    ) -> Result<(), IpcError> {
        if method == "events.publish" {
            if !self.has_peer_role(Role::Source) {
                return method_not_found(method);
            }
            validate_fixed_request(method, params)?;
            return self.validate_published_type(params);
        }
        if is_client_method(method) {
            if !self.has_peer_role(Role::Client) {
                return method_not_found(method);
            }
            return validate_fixed_request(method, params);
        }
        method_not_found(method)
    }

    fn validate_peer_incoming(
        &self,
        method: &str,
        params: &Map<String, Value>,
    ) -> Result<(), IpcError> {
        if !self.has_peer_role(Role::Provider) || !self.peer_declares_method(method) {
            return method_not_found(method);
        }
        validate_direct_request(params)
    }

    fn validate_published_type(&self, params: &Map<String, Value>) -> Result<(), IpcError> {
        let Some(event_type) = params.get("type").and_then(Value::as_str) else {
            return Err(IpcError::new(
                INVALID_PARAMS,
                "`type` must be a non-empty string",
            ));
        };
        let Some(peer_params) = &self.peer_params else {
            return Err(IpcError::new(
                INTERNAL_ERROR,
                "the peer profile is unavailable",
            ));
        };
        let Some(event_types) = &peer_params.event_types else {
            return Err(IpcError::new(
                METHOD_NOT_FOUND,
                "the peer did not declare event types",
            ));
        };
        for declared in event_types {
            if declared.event_type == event_type {
                return Ok(());
            }
        }
        Err(IpcError::new(
            METHOD_NOT_FOUND,
            format!("event type {event_type:?} was not declared"),
        ))
    }

    fn has_peer_role(&self, expected: Role) -> bool {
        let Some(params) = &self.peer_params else {
            return false;
        };
        for role in &params.roles {
            if *role == expected {
                return true;
            }
        }
        false
    }

    fn peer_declares_method(&self, method: &str) -> bool {
        let Some(params) = &self.peer_params else {
            return false;
        };
        let Some(methods) = &params.methods else {
            return false;
        };
        for declared in methods {
            if declared.name == method {
                return true;
            }
        }
        false
    }

    fn reject_request(&self, request: Request, error: IpcError) -> Vec<Action> {
        let response = ErrorResponse::new(Some(request.id), error.clone().into());
        vec![
            Action::Send(Message::Error(response)),
            Action::Violation(error),
        ]
    }

    fn reject_request_with_code(
        &self,
        request: Request,
        error: IpcError,
        stable_code: &str,
    ) -> Vec<Action> {
        let response = ErrorResponse::new(
            Some(request.id),
            ErrorObject::protocol(error.code, stable_code, error.message.clone()),
        );
        vec![
            Action::Send(Message::Error(response)),
            Action::Violation(error),
        ]
    }
}

fn is_client_method(method: &str) -> bool {
    method == "events.list"
        || method == "agents.list"
        || method == "project.reload"
        || method == "session.start"
        || method == "session.send"
        || method == "session.status"
        || method == "session.list"
        || method == "session.cancel"
}

fn method_not_found<T>(method: &str) -> Result<T, IpcError> {
    Err(IpcError::new(
        METHOD_NOT_FOUND,
        format!("method {method:?} is not available for this role and direction"),
    ))
}

fn with_close(mut actions: Vec<Action>, reason: &str) -> Vec<Action> {
    actions.push(Action::Close {
        reason: Some(reason.to_string()),
    });
    actions
}
