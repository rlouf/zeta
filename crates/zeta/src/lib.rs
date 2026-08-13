//! Composes operating-system services for the native Zeta application.

use std::fmt;

use serde_json::{Map, Value};
use zeta_agent::{AgentProposal, AgentRunResult, RunStopReason};
use zeta_dispatch::{AttemptCompletion, AttemptCompletionDisposition, AttemptControl};

pub mod process_executor;
pub mod runtime_services;

pub use process_executor::{ProcessExecutor, ProcessExecutorConfig, ProcessLaunch};
pub use runtime_services::{
    prepare_agent, CallbackDraftRecorder, CallbackObserver, CancellationToken, ExecutorSelection,
    InvocationInputs, PrepareAgentError, PrepareAgentErrorKind, PreparedAgent, SystemClock,
    UuidIdSource,
};

/// Classifies a failure while handing an agent result to Dispatch.
///
/// # Examples
///
/// ```
/// let kind = zeta::CompletionHandoffErrorKind::MalformedUsage;
/// assert_eq!(kind.reason(), "malformed_usage");
/// ```
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum CompletionHandoffErrorKind {
    /// A content promotion has no atomic Dispatch operation yet.
    UnsupportedContentPromotion,
    /// A platform-sized request position does not fit Dispatch's durable type.
    ControlPositionOverflow,
    /// Agent telemetry carries a non-object usage value.
    MalformedUsage,
}

impl CompletionHandoffErrorKind {
    /// Returns a stable machine-readable reason.
    pub fn reason(self) -> &'static str {
        match self {
            CompletionHandoffErrorKind::UnsupportedContentPromotion => {
                "unsupported_content_promotion"
            }
            CompletionHandoffErrorKind::ControlPositionOverflow => "control_position_overflow",
            CompletionHandoffErrorKind::MalformedUsage => "malformed_usage",
        }
    }
}

/// Reports why an agent result cannot become a Dispatch completion.
///
/// # Examples
///
/// ```
/// # fn inspect(error: &zeta::CompletionHandoffError) {
/// assert!(!error.detail().is_empty());
/// let _kind = error.kind();
/// # }
/// ```
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct CompletionHandoffError {
    kind: CompletionHandoffErrorKind,
    detail: String,
}

impl CompletionHandoffError {
    fn new(kind: CompletionHandoffErrorKind, detail: impl Into<String>) -> Self {
        CompletionHandoffError {
            kind,
            detail: detail.into(),
        }
    }

    /// Returns the stable failure class.
    pub fn kind(&self) -> CompletionHandoffErrorKind {
        self.kind
    }

    /// Returns the stable machine-readable reason.
    pub fn reason(&self) -> &'static str {
        self.kind.reason()
    }

    /// Returns the human-readable failure detail.
    pub fn detail(&self) -> &str {
        &self.detail
    }
}

impl fmt::Display for CompletionHandoffError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "{}: {}", self.reason(), self.detail)
    }
}

impl std::error::Error for CompletionHandoffError {}

/// Converts one successful agent result into a typed Dispatch completion.
///
/// # Examples
///
/// ```
/// let result = zeta_agent::AgentRunResult {
///     final_answer: "done".to_owned(),
///     ..zeta_agent::AgentRunResult::default()
/// };
/// let completion = zeta::attempt_completion("2026-08-12T10:00:01Z", &result)?;
/// assert_eq!(completion.metadata()["final_answer"], "done");
/// # Ok::<(), zeta::CompletionHandoffError>(())
/// ```
///
/// # Errors
///
/// Returns [`CompletionHandoffError`] when usage telemetry is not an object, a
/// control position does not fit Dispatch's durable `u64`, or the result
/// contains a content-promotion proposal that Dispatch cannot commit atomically.
pub fn attempt_completion(
    finished_at: impl Into<String>,
    result: &AgentRunResult,
) -> Result<AttemptCompletion, CompletionHandoffError> {
    let mut metadata = Map::new();
    metadata.insert(
        "final_answer".to_owned(),
        Value::String(result.final_answer.clone()),
    );
    if let Some(final_object_id) = &result.final_object_id {
        metadata.insert(
            "final_object_id".to_owned(),
            Value::String(final_object_id.clone()),
        );
    }
    if let Some(stop_reason) = result.stop_reason {
        let stop_reason = match stop_reason {
            RunStopReason::Finished => "finished",
            RunStopReason::ToolStop => "tool_stop",
            RunStopReason::MaxModelCalls => "max_model_calls",
        };
        metadata.insert(
            "stop_reason".to_owned(),
            Value::String(stop_reason.to_owned()),
        );
    }
    if !result.events.is_empty() {
        let mut events = Vec::new();
        for event in &result.events {
            events.push(draft_event_value(event));
        }
        metadata.insert("events".to_owned(), Value::Array(events));
    }
    if let Some(usage) = result.telemetry.get("usage") {
        let Value::Object(usage) = usage else {
            return Err(CompletionHandoffError::new(
                CompletionHandoffErrorKind::MalformedUsage,
                "telemetry.usage must be a JSON object",
            ));
        };
        metadata.insert("usage".to_owned(), Value::Object(usage.clone()));
    }

    let mut controls = Vec::new();
    for proposal in &result.proposals {
        match proposal {
            AgentProposal::Publish {
                handle,
                event_type,
                payload,
                at,
                position,
            } => {
                let position = completion_position(*position)?;
                controls.push(AttemptControl::publish(
                    handle,
                    event_type,
                    payload.clone(),
                    at.clone(),
                    position,
                ));
            }
            AgentProposal::Wait {
                handle,
                event_type,
                fields,
                deadline,
                position,
            } => {
                let position = completion_position(*position)?;
                controls.push(AttemptControl::wait(
                    handle,
                    event_type,
                    fields.clone(),
                    deadline.clone(),
                    position,
                ));
            }
            AgentProposal::Cancel {
                handle,
                reason,
                source_agent_id,
                source_session_id,
                position,
            } => {
                let position = completion_position(*position)?;
                controls.push(AttemptControl::cancel(
                    handle,
                    reason.clone(),
                    source_agent_id,
                    source_session_id,
                    position,
                ));
            }
            AgentProposal::ContentPromotion {
                scope,
                key,
                object_id: _object_id,
                expected_head: _expected_head,
                expected_object_id: _expected_object_id,
                source_head: _source_head,
                reason: _reason,
            } => {
                return Err(CompletionHandoffError::new(
                    CompletionHandoffErrorKind::UnsupportedContentPromotion,
                    format!("content promotion {scope}/{key} has no Dispatch commit operation"),
                ));
            }
        }
    }

    Ok(AttemptCompletion::new(
        finished_at,
        AttemptCompletionDisposition::Succeeded,
        metadata,
        controls,
    ))
}

fn completion_position(position: usize) -> Result<u64, CompletionHandoffError> {
    u64::try_from(position).map_err(|_error| {
        CompletionHandoffError::new(
            CompletionHandoffErrorKind::ControlPositionOverflow,
            format!("control position {position} does not fit u64"),
        )
    })
}

fn draft_event_value(event: &zeta_journal::DraftEvent) -> Value {
    let mut value = Map::new();
    value.insert("type".to_owned(), Value::String(event.event_type.clone()));
    value.insert("source".to_owned(), Value::String(event.source.clone()));
    value.insert("payload".to_owned(), Value::Object(event.payload.clone()));
    value.insert(
        "idempotency_key".to_owned(),
        optional_string_value(event.idempotency_key.as_ref()),
    );
    value.insert(
        "caused_by".to_owned(),
        optional_string_value(event.caused_by.as_ref()),
    );
    value.insert(
        "session_id".to_owned(),
        optional_string_value(event.session_id.as_ref()),
    );
    value.insert(
        "run_id".to_owned(),
        optional_string_value(event.run_id.as_ref()),
    );
    value.insert(
        "turn_id".to_owned(),
        optional_string_value(event.turn_id.as_ref()),
    );
    Value::Object(value)
}

fn optional_string_value(value: Option<&String>) -> Value {
    match value {
        Some(value) => Value::String(value.clone()),
        None => Value::Null,
    }
}

/// Converts a verified authored project into deterministic runtime routes.
///
/// Disabled agents are omitted. The manifest's slug order becomes the stable
/// route order, and authored accepted event types retain exact-match semantics.
///
/// # Examples
///
/// ```
/// # fn convert(
/// #     manifest: &zeta_authoring::ProjectManifest,
/// # ) -> Result<(), zeta_authoring::AuthoringError> {
/// let routes = zeta::routes_from_project(manifest)?;
/// assert!(routes.iter().all(|route| !route.agent_id().is_empty()));
/// # Ok(())
/// # }
/// ```
///
/// # Errors
///
/// Returns [`zeta_authoring::AuthoringError`] when the manifest body does not
/// match its canonical project generation or violates its schema contract.
pub fn routes_from_project(
    manifest: &zeta_authoring::ProjectManifest,
) -> Result<Vec<zeta_dispatch::Route>, zeta_authoring::AuthoringError> {
    zeta_authoring::verify_project_manifest(manifest)?;
    let mut routes = Vec::new();
    for spec in manifest.agents.values() {
        if !spec.enabled {
            continue;
        }
        let mut accepts = Vec::new();
        for event_type in &spec.accepts {
            accepts.push(zeta_dispatch::EventPattern::exact(event_type));
        }
        let session = if spec.session == "shared" {
            zeta_dispatch::SessionRule::Shared
        } else if spec.session == "per-event" {
            zeta_dispatch::SessionRule::PerEvent
        } else {
            zeta_dispatch::SessionRule::Template(spec.session.clone())
        };
        routes.push(zeta_dispatch::Route::new(
            &spec.slug,
            accepts,
            session,
            spec.locks.clone(),
            Some(manifest.id.to_string()),
        ));
    }
    Ok(routes)
}
