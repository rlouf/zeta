use rusqlite::{params, Connection, TransactionBehavior};
use zeta_journal::{verify, Event, HeadExpectation};

use super::attempts::index_attempt;
use super::effects::index_effect;
use super::journal::load_entries;
use super::resources::{index_deferred_publication, index_wait};
use super::routing::{
    index_pending_queue_item, index_queue_item, index_queue_item_cancel_requested,
};
use super::{
    database_error, Dispatch, DispatchError, CREATE_PROJECTIONS, DROP_PROJECTIONS, PROJECTION_EPOCH,
};

impl Dispatch {
    /// Rebuilds every event-sourced projection from the ordered journal.
    ///
    /// Live claims and locks are coordination state, so rebuild discards them.
    /// A replayed claimed item becomes pending when still unbound and available
    /// when already bound. Historical running attempts remain running.
    ///
    /// # Errors
    ///
    /// Returns [`DispatchError`] when schema reset, journal decoding, lifecycle
    /// validation, or replay fails.
    pub fn rebuild_projections(&mut self) -> Result<usize, DispatchError> {
        let transaction = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(|error| database_error("begin projection rebuild", error))?;
        let replayed = rebuild_projections_in_transaction(&transaction)?;
        transaction
            .execute(
                "UPDATE dispatch_schema SET projection_epoch = ?1 WHERE singleton = 1",
                params![PROJECTION_EPOCH],
            )
            .map_err(|error| database_error("record projection epoch", error))?;
        transaction
            .commit()
            .map_err(|error| database_error("commit projection rebuild", error))?;
        Ok(replayed)
    }
}

pub(super) fn index_event(connection: &Connection, event: &Event) -> Result<(), DispatchError> {
    if event.event_type.starts_with("runtime.effect.") {
        return index_effect(connection, event);
    }
    if event.event_type.starts_with("runtime.wait.") {
        return index_wait(connection, event);
    }
    if event
        .event_type
        .starts_with("runtime.deferred_publication.")
    {
        return index_deferred_publication(connection, event);
    }
    if event.event_type == "runtime.queue_item.cancel_requested" {
        return index_queue_item_cancel_requested(connection, event);
    }
    if event.event_type.starts_with("runtime.queue_item.") {
        return index_queue_item(connection, event);
    }
    if event.event_type.starts_with("runtime.attempt.") {
        return index_attempt(connection, event);
    }
    if is_queueable_event(event) {
        index_pending_queue_item(connection, event)?;
    }
    Ok(())
}

fn is_queueable_event(event: &Event) -> bool {
    for prefix in ["runtime.", "zeta."] {
        if event.event_type.starts_with(prefix) {
            return false;
        }
    }
    true
}

pub(super) fn rebuild_projections_in_transaction(
    connection: &Connection,
) -> Result<usize, DispatchError> {
    let entries = load_entries(connection, false)?;
    verify(&entries, HeadExpectation::Unanchored).map_err(DispatchError::Verification)?;
    connection
        .execute_batch(DROP_PROJECTIONS)
        .map_err(|error| database_error("drop projections", error))?;
    connection
        .execute_batch(CREATE_PROJECTIONS)
        .map_err(|error| database_error("create projections", error))?;
    for entry in &entries {
        index_event(connection, &entry.event)?;
    }
    connection
        .execute(
            "UPDATE queue_items
             SET status = CASE
                    WHEN target_agent = '' THEN 'pending'
                    ELSE 'available'
                 END
             WHERE status = 'claimed'",
            [],
        )
        .map_err(|error| database_error("recover claimed queue items", error))?;
    connection
        .execute(
            "UPDATE attempts SET claim_token = NULL, heartbeat_at = NULL",
            [],
        )
        .map_err(|error| database_error("clear replayed attempt ownership", error))?;
    connection
        .execute(
            "UPDATE deferred_publications SET status = 'pending' WHERE status = 'claimed'",
            [],
        )
        .map_err(|error| database_error("recover claimed deferred publications", error))?;
    Ok(entries.len())
}
