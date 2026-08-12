#![doc = include_str!("../README.md")]

mod dispatch;
mod identity;
mod routing;
mod sqlite;
mod state;

pub use dispatch::{
    Attempt, AttemptCompletion, AttemptCompletionDisposition, AttemptControl, AttemptFailure,
    CancellationFinalizationIdentities, CancellationIdentities, CancellationOutcome,
    CancellationStatus, Effect, EffectDeliverySemantics, EffectStatus, LockLease, QueueClaim,
    QueueItem, RecurringSchedule, RecurringScheduleActivation, RecurringScheduleStatus,
    RecurringScheduleTick, ResourceCancellationOutcome, ResourceCancellationStatus, ResourceKind,
    RoutingOutcome, RuntimeEventIdentity, ScheduleTickStatus, ScheduledEvent, ScheduledEventStatus,
    Session, SessionActiveWait, SessionActivityStatus, SessionLatestRun, SessionMessageIdentities,
    SessionMessageRequest, StartedAttempt, SubmittedSessionMessage, Wait, WaitStatus,
};
pub use identity::{
    attempt_id, attempt_idempotency_key, derived_run_id, effect_key, pending_queue_item_id,
    publish_event_handle, queue_item_attempt_idempotency_key, queue_item_id,
    queue_item_idempotency_key, run_id_for_attempt, safe_agent_id, unhandled_queue_item_id,
    unhandled_queue_item_idempotency_key, wait_handle, AttemptId, ClaimToken, PublishHandle,
    QueueItemId, RunId, RuntimeIdParseError, SessionId, WaitHandle,
};
pub use routing::{route_event, EventPattern, Route, RouteDecision, SessionError, SessionRule};
pub use sqlite::{Dispatch, DispatchError};
pub use state::{
    classify_error_code, AttemptStatus, DispatchErrorCode, FailureClass, QueueItemStatus,
    RetryPolicy, RetryPolicyError, StateParseError, TransitionError,
};
