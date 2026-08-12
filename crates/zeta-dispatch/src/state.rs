//! Exhaustive runtime states and bounded retry behavior.

use std::fmt;
use std::str::FromStr;

use serde::{Deserialize, Serialize};

/// Describes every durable queue-item state.
///
/// # Examples
///
/// ```
/// use zeta_dispatch::QueueItemStatus;
///
/// QueueItemStatus::validate_transition(
///     Some(QueueItemStatus::Available),
///     QueueItemStatus::Claimed,
/// )?;
/// # Ok::<(), zeta_dispatch::TransitionError>(())
/// ```
#[derive(Clone, Copy, Debug, Deserialize, Eq, Hash, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum QueueItemStatus {
    /// Routing has not bound the ingress event yet.
    Pending,
    /// Work may be claimed.
    Available,
    /// One worker owns a live claim.
    Claimed,
    /// The invocation and its accepted outputs committed.
    Completed,
    /// One attempt failed before a retry decision.
    Failed,
    /// Cancellation terminated the work.
    Cancelled,
    /// A failed item is waiting for its retry time.
    RetryScheduled,
    /// No later attempt may run after a terminal failure.
    DeadLettered,
    /// No route accepted the ingress event.
    Unhandled,
}

impl QueueItemStatus {
    /// Lists every state in its stable vocabulary order.
    pub const ALL: [QueueItemStatus; 9] = [
        QueueItemStatus::Pending,
        QueueItemStatus::Available,
        QueueItemStatus::Claimed,
        QueueItemStatus::Completed,
        QueueItemStatus::Failed,
        QueueItemStatus::Cancelled,
        QueueItemStatus::RetryScheduled,
        QueueItemStatus::DeadLettered,
        QueueItemStatus::Unhandled,
    ];

    /// Reports whether one queue transition belongs to the runtime state machine.
    pub fn can_transition(previous: Option<QueueItemStatus>, current: QueueItemStatus) -> bool {
        match previous {
            None => {
                current == QueueItemStatus::Pending
                    || current == QueueItemStatus::Available
                    || current == QueueItemStatus::Unhandled
            }
            Some(QueueItemStatus::Pending) => {
                current == QueueItemStatus::Available
                    || current == QueueItemStatus::Claimed
                    || current == QueueItemStatus::Unhandled
            }
            Some(QueueItemStatus::Available) => {
                current == QueueItemStatus::Claimed || current == QueueItemStatus::Cancelled
            }
            Some(QueueItemStatus::Claimed) => {
                current == QueueItemStatus::Available
                    || current == QueueItemStatus::Completed
                    || current == QueueItemStatus::Failed
                    || current == QueueItemStatus::Cancelled
                    || current == QueueItemStatus::DeadLettered
            }
            Some(QueueItemStatus::RetryScheduled) => {
                current == QueueItemStatus::Available || current == QueueItemStatus::Cancelled
            }
            Some(QueueItemStatus::Failed) => {
                current == QueueItemStatus::RetryScheduled
                    || current == QueueItemStatus::DeadLettered
            }
            Some(QueueItemStatus::Completed) => false,
            Some(QueueItemStatus::Cancelled) => false,
            Some(QueueItemStatus::DeadLettered) => false,
            Some(QueueItemStatus::Unhandled) => false,
        }
    }

    /// Validates one queue transition and retains both states on failure.
    ///
    /// # Errors
    ///
    /// Returns [`TransitionError`] when the requested edge is not legal.
    pub fn validate_transition(
        previous: Option<QueueItemStatus>,
        current: QueueItemStatus,
    ) -> Result<(), TransitionError> {
        if QueueItemStatus::can_transition(previous, current) {
            return Ok(());
        }
        Err(TransitionError::Queue { previous, current })
    }
}

impl fmt::Display for QueueItemStatus {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        let text = match self {
            QueueItemStatus::Pending => "pending",
            QueueItemStatus::Available => "available",
            QueueItemStatus::Claimed => "claimed",
            QueueItemStatus::Completed => "completed",
            QueueItemStatus::Failed => "failed",
            QueueItemStatus::Cancelled => "cancelled",
            QueueItemStatus::RetryScheduled => "retry_scheduled",
            QueueItemStatus::DeadLettered => "dead_lettered",
            QueueItemStatus::Unhandled => "unhandled",
        };
        formatter.write_str(text)
    }
}

impl FromStr for QueueItemStatus {
    type Err = StateParseError;

    fn from_str(text: &str) -> Result<Self, Self::Err> {
        let status = if text == "pending" {
            QueueItemStatus::Pending
        } else if text == "available" {
            QueueItemStatus::Available
        } else if text == "claimed" {
            QueueItemStatus::Claimed
        } else if text == "completed" {
            QueueItemStatus::Completed
        } else if text == "failed" {
            QueueItemStatus::Failed
        } else if text == "cancelled" {
            QueueItemStatus::Cancelled
        } else if text == "retry_scheduled" {
            QueueItemStatus::RetryScheduled
        } else if text == "dead_lettered" {
            QueueItemStatus::DeadLettered
        } else if text == "unhandled" {
            QueueItemStatus::Unhandled
        } else {
            return Err(StateParseError {
                resource: "queue item",
                value: text.to_owned(),
            });
        };
        Ok(status)
    }
}

/// Describes every durable attempt state.
#[derive(Clone, Copy, Debug, Deserialize, Eq, Hash, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum AttemptStatus {
    /// Execution has begun.
    Running,
    /// The invocation and accepted outputs committed.
    Completed,
    /// Execution or result validation failed.
    Failed,
    /// Cooperative cancellation stopped execution.
    Cancelled,
}

impl AttemptStatus {
    /// Lists every state in its stable vocabulary order.
    pub const ALL: [AttemptStatus; 4] = [
        AttemptStatus::Running,
        AttemptStatus::Completed,
        AttemptStatus::Failed,
        AttemptStatus::Cancelled,
    ];

    /// Reports whether one attempt transition belongs to the runtime state machine.
    pub fn can_transition(previous: Option<AttemptStatus>, current: AttemptStatus) -> bool {
        match previous {
            None => current == AttemptStatus::Running,
            Some(AttemptStatus::Running) => {
                current == AttemptStatus::Completed
                    || current == AttemptStatus::Failed
                    || current == AttemptStatus::Cancelled
            }
            Some(AttemptStatus::Completed) => false,
            Some(AttemptStatus::Failed) => false,
            Some(AttemptStatus::Cancelled) => false,
        }
    }

    /// Validates one attempt transition and retains both states on failure.
    ///
    /// # Errors
    ///
    /// Returns [`TransitionError`] when the requested edge is not legal.
    pub fn validate_transition(
        previous: Option<AttemptStatus>,
        current: AttemptStatus,
    ) -> Result<(), TransitionError> {
        if AttemptStatus::can_transition(previous, current) {
            return Ok(());
        }
        Err(TransitionError::Attempt { previous, current })
    }
}

impl fmt::Display for AttemptStatus {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        let text = match self {
            AttemptStatus::Running => "running",
            AttemptStatus::Completed => "completed",
            AttemptStatus::Failed => "failed",
            AttemptStatus::Cancelled => "cancelled",
        };
        formatter.write_str(text)
    }
}

impl FromStr for AttemptStatus {
    type Err = StateParseError;

    fn from_str(text: &str) -> Result<Self, Self::Err> {
        let status = if text == "running" {
            AttemptStatus::Running
        } else if text == "completed" {
            AttemptStatus::Completed
        } else if text == "failed" {
            AttemptStatus::Failed
        } else if text == "cancelled" {
            AttemptStatus::Cancelled
        } else {
            return Err(StateParseError {
                resource: "attempt",
                value: text.to_owned(),
            });
        };
        Ok(status)
    }
}

/// Reports an unknown state string without conflating queue and attempt vocabularies.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct StateParseError {
    resource: &'static str,
    value: String,
}

impl StateParseError {
    /// Returns the state-machine resource whose value was unknown.
    pub fn resource(&self) -> &'static str {
        self.resource
    }

    /// Returns the rejected state text.
    pub fn value(&self) -> &str {
        &self.value
    }
}

impl fmt::Display for StateParseError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            formatter,
            "unsupported {} status {:?}",
            self.resource, self.value
        )
    }
}

impl std::error::Error for StateParseError {}

/// Retains the exact illegal edge rejected by a runtime state machine.
#[derive(Clone, Debug, Eq, PartialEq)]
pub enum TransitionError {
    /// A queue item cannot take the requested edge.
    Queue {
        /// Carries the prior state, or `None` before creation.
        previous: Option<QueueItemStatus>,
        /// Carries the rejected next state.
        current: QueueItemStatus,
    },
    /// An attempt cannot take the requested edge.
    Attempt {
        /// Carries the prior state, or `None` before creation.
        previous: Option<AttemptStatus>,
        /// Carries the rejected next state.
        current: AttemptStatus,
    },
}

impl fmt::Display for TransitionError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            TransitionError::Queue { previous, current } => {
                write!(
                    formatter,
                    "invalid queue item transition: {previous:?} -> {current}"
                )
            }
            TransitionError::Attempt { previous, current } => {
                write!(
                    formatter,
                    "invalid attempt transition: {previous:?} -> {current}"
                )
            }
        }
    }
}

impl std::error::Error for TransitionError {}

/// Classifies whether a failed invocation may safely run again.
#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum FailureClass {
    /// Another attempt may run under the configured policy.
    Retryable,
    /// No later attempt may run.
    Permanent,
}

/// Names every structured failure class emitted by Dispatch execution.
#[derive(Clone, Copy, Debug, Deserialize, Eq, Hash, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum DispatchErrorCode {
    /// The stored or authored agent declaration is invalid.
    AgentSpecInvalid,
    /// The triggering event does not satisfy its declared payload contract.
    MalformedEventPayload,
    /// The model provider exceeded its response deadline.
    ProviderTimeout,
    /// A transport failed before a safe result was observed.
    NetworkError,
    /// An invoked tool returned a retryable failure.
    ToolFailed,
    /// Agent execution failed without a narrower structured class.
    AgentExecutionFailed,
    /// A retry-safe effect delivery failed.
    EffectDeliveryFailed,
    /// An unsafe effect may have happened without a durable outcome.
    UnsafeEffectAmbiguous,
}

impl DispatchErrorCode {
    /// Lists the complete structured error-code vocabulary.
    pub const ALL: [DispatchErrorCode; 8] = [
        DispatchErrorCode::AgentSpecInvalid,
        DispatchErrorCode::MalformedEventPayload,
        DispatchErrorCode::ProviderTimeout,
        DispatchErrorCode::NetworkError,
        DispatchErrorCode::ToolFailed,
        DispatchErrorCode::AgentExecutionFailed,
        DispatchErrorCode::EffectDeliveryFailed,
        DispatchErrorCode::UnsafeEffectAmbiguous,
    ];
}

impl fmt::Display for DispatchErrorCode {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        let text = match self {
            DispatchErrorCode::AgentSpecInvalid => "agent_spec_invalid",
            DispatchErrorCode::MalformedEventPayload => "malformed_event_payload",
            DispatchErrorCode::ProviderTimeout => "provider_timeout",
            DispatchErrorCode::NetworkError => "network_error",
            DispatchErrorCode::ToolFailed => "tool_failed",
            DispatchErrorCode::AgentExecutionFailed => "agent_execution_failed",
            DispatchErrorCode::EffectDeliveryFailed => "effect_delivery_failed",
            DispatchErrorCode::UnsafeEffectAmbiguous => "unsafe_effect_ambiguous",
        };
        formatter.write_str(text)
    }
}

impl FromStr for DispatchErrorCode {
    type Err = StateParseError;

    fn from_str(text: &str) -> Result<Self, Self::Err> {
        let code = if text == "agent_spec_invalid" {
            DispatchErrorCode::AgentSpecInvalid
        } else if text == "malformed_event_payload" {
            DispatchErrorCode::MalformedEventPayload
        } else if text == "provider_timeout" {
            DispatchErrorCode::ProviderTimeout
        } else if text == "network_error" {
            DispatchErrorCode::NetworkError
        } else if text == "tool_failed" {
            DispatchErrorCode::ToolFailed
        } else if text == "agent_execution_failed" {
            DispatchErrorCode::AgentExecutionFailed
        } else if text == "effect_delivery_failed" {
            DispatchErrorCode::EffectDeliveryFailed
        } else if text == "unsafe_effect_ambiguous" {
            DispatchErrorCode::UnsafeEffectAmbiguous
        } else {
            return Err(StateParseError {
                resource: "dispatch error code",
                value: text.to_owned(),
            });
        };
        Ok(code)
    }
}

/// Classifies a structured dispatch error without matching display text.
pub fn classify_error_code(error_code: DispatchErrorCode) -> FailureClass {
    match error_code {
        DispatchErrorCode::AgentSpecInvalid => FailureClass::Permanent,
        DispatchErrorCode::MalformedEventPayload => FailureClass::Permanent,
        DispatchErrorCode::ProviderTimeout => FailureClass::Retryable,
        DispatchErrorCode::NetworkError => FailureClass::Retryable,
        DispatchErrorCode::ToolFailed => FailureClass::Retryable,
        DispatchErrorCode::AgentExecutionFailed => FailureClass::Retryable,
        DispatchErrorCode::EffectDeliveryFailed => FailureClass::Retryable,
        DispatchErrorCode::UnsafeEffectAmbiguous => FailureClass::Permanent,
    }
}

/// Configures bounded exponential retry delays.
///
/// # Examples
///
/// ```
/// let policy = zeta_dispatch::RetryPolicy::new(3, 2.0, 2.0, 10.0)?;
/// assert_eq!(policy.delay_ms(1)?, 2_000);
/// assert_eq!(policy.delay_ms(3)?, 8_000);
/// # Ok::<(), zeta_dispatch::RetryPolicyError>(())
/// ```
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct RetryPolicy {
    max_attempts: u32,
    backoff_base_seconds: f64,
    backoff_factor: f64,
    backoff_max_seconds: f64,
}

impl RetryPolicy {
    /// Creates a policy after validating its bounds.
    ///
    /// # Errors
    ///
    /// Returns [`RetryPolicyError`] for a zero attempt count, a negative or
    /// non-finite duration, or a factor below one.
    pub fn new(
        max_attempts: u32,
        backoff_base_seconds: f64,
        backoff_factor: f64,
        backoff_max_seconds: f64,
    ) -> Result<Self, RetryPolicyError> {
        if max_attempts == 0 {
            return Err(RetryPolicyError::MaxAttempts);
        }
        if !backoff_base_seconds.is_finite() || backoff_base_seconds < 0.0 {
            return Err(RetryPolicyError::BackoffBase);
        }
        if !backoff_factor.is_finite() || backoff_factor < 1.0 {
            return Err(RetryPolicyError::BackoffFactor);
        }
        if !backoff_max_seconds.is_finite() || backoff_max_seconds < 0.0 {
            return Err(RetryPolicyError::BackoffMaximum);
        }
        Ok(RetryPolicy {
            max_attempts,
            backoff_base_seconds,
            backoff_factor,
            backoff_max_seconds,
        })
    }

    /// Returns the largest attempt number allowed by the policy.
    pub fn max_attempts(&self) -> u32 {
        self.max_attempts
    }

    /// Reports whether a failure at this attempt number leaves another attempt.
    pub fn permits_retry_after(&self, attempt_number: u32) -> bool {
        attempt_number < self.max_attempts
    }

    /// Returns the bounded delay following one positive attempt number.
    ///
    /// # Errors
    ///
    /// Returns [`RetryPolicyError::AttemptNumber`] for attempt zero.
    pub fn delay_seconds(&self, attempt_number: u32) -> Result<f64, RetryPolicyError> {
        if attempt_number == 0 {
            return Err(RetryPolicyError::AttemptNumber);
        }
        let exponent = f64::from(attempt_number - 1);
        let delay = self.backoff_base_seconds * self.backoff_factor.powf(exponent);
        Ok(delay.min(self.backoff_max_seconds))
    }

    /// Returns the bounded delay in whole milliseconds.
    ///
    /// # Errors
    ///
    /// Returns [`RetryPolicyError`] for attempt zero or a duration outside the
    /// unsigned 64-bit millisecond range.
    pub fn delay_ms(&self, attempt_number: u32) -> Result<u64, RetryPolicyError> {
        let milliseconds = self.delay_seconds(attempt_number)? * 1_000.0;
        if milliseconds >= u64::MAX as f64 {
            return Err(RetryPolicyError::DelayOverflow);
        }
        Ok(milliseconds as u64)
    }

    /// Returns stable queue-key jitter within an inclusive spread.
    pub fn deterministic_jitter_seconds(&self, key: &str, spread_seconds: f64) -> f64 {
        if spread_seconds <= 0.0 {
            return 0.0;
        }
        let mut bucket = 0_u64;
        for byte in key.as_bytes() {
            bucket += u64::from(*byte);
        }
        let bucket = bucket % 10_000;
        spread_seconds * (bucket as f64 / 10_000.0)
    }
}

impl Default for RetryPolicy {
    fn default() -> Self {
        RetryPolicy {
            max_attempts: 3,
            backoff_base_seconds: 5.0,
            backoff_factor: 2.0,
            backoff_max_seconds: 300.0,
        }
    }
}

/// Reports why a retry policy or delay request is invalid.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum RetryPolicyError {
    /// At least one attempt is required.
    MaxAttempts,
    /// The base delay must be finite and non-negative.
    BackoffBase,
    /// The exponential factor must be finite and at least one.
    BackoffFactor,
    /// The maximum delay must be finite and non-negative.
    BackoffMaximum,
    /// Attempt numbers start at one.
    AttemptNumber,
    /// The millisecond delay does not fit the public representation.
    DelayOverflow,
}

impl fmt::Display for RetryPolicyError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        let message = match self {
            RetryPolicyError::MaxAttempts => "max attempts must be positive",
            RetryPolicyError::BackoffBase => "backoff base must be finite and non-negative",
            RetryPolicyError::BackoffFactor => "backoff factor must be finite and at least one",
            RetryPolicyError::BackoffMaximum => "backoff maximum must be finite and non-negative",
            RetryPolicyError::AttemptNumber => "attempt number must be positive",
            RetryPolicyError::DelayOverflow => "retry delay milliseconds overflow u64",
        };
        formatter.write_str(message)
    }
}

impl std::error::Error for RetryPolicyError {}
