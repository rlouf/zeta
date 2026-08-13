//! Reactively routes durable native runtime ingress.

use std::collections::BTreeMap;
use std::fmt;
use std::path::Path;
use std::sync::{mpsc, Mutex};
use std::thread;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use serde::{Deserialize, Serialize};
use tokio::sync::watch;
use uuid::Uuid;
use zeta_dispatch::{route_event, Dispatch, Route, RuntimeEventIdentity};
use zeta_journal::{DraftEvent, Event};

use crate::ProjectGeneration;

const DUE_ADVANCE_LIMIT: usize = 128;

/// Reports a native reactive-runtime failure.
#[derive(Debug)]
pub struct ReactiveRuntimeError {
    detail: String,
}

impl ReactiveRuntimeError {
    fn new(detail: impl Into<String>) -> Self {
        Self {
            detail: detail.into(),
        }
    }
}

impl fmt::Display for ReactiveRuntimeError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.detail)
    }
}

impl std::error::Error for ReactiveRuntimeError {}

/// Reports the result of one durably accepted ingress event.
#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
pub struct IngressResult {
    /// Contains the retained event identity.
    pub event_id: String,
    /// Reports whether this call appended a new durable event.
    pub inserted: bool,
    /// Counts the durable agent routes retained for the event.
    pub route_count: usize,
}

/// Reports durable work that the reactive host can observe.
#[derive(Clone, Debug, Default, Deserialize, PartialEq, Serialize)]
pub struct ReactiveRuntimeStatus {
    /// Identifies the latest local wake epoch.
    pub wake_epoch: u64,
    /// Counts queue items by durable lifecycle state.
    pub queue: BTreeMap<String, usize>,
    /// Counts active waits.
    pub active_waits: usize,
    /// Counts pending deferred publications.
    pub pending_publications: usize,
    /// Contains the next durable maintenance deadline when one exists.
    pub next_deadline_ms: Option<i64>,
}

/// Carries a coalescing local notification after durable work commits.
#[derive(Clone)]
pub struct RuntimeWake {
    sender: watch::Sender<u64>,
}

impl RuntimeWake {
    fn new() -> (Self, watch::Receiver<u64>) {
        let (sender, receiver) = watch::channel(0_u64);
        (Self { sender }, receiver)
    }

    /// Returns a receiver for future wake epochs.
    pub fn subscribe(&self) -> watch::Receiver<u64> {
        self.sender.subscribe()
    }

    fn signal(&self) -> u64 {
        let next = self.sender.borrow().saturating_add(1);
        self.sender.send_replace(next);
        next
    }

    fn epoch(&self) -> u64 {
        *self.sender.borrow()
    }
}

/// Owns the native dispatch actor and its durable ingress path.
pub struct ReactiveRuntime {
    sender: mpsc::Sender<RuntimeCommand>,
    wake: RuntimeWake,
    thread: Mutex<Option<thread::JoinHandle<()>>>,
}

impl ReactiveRuntime {
    /// Opens a durable dispatch actor for one active project generation.
    pub fn start(
        database_path: impl AsRef<Path>,
        generation: ProjectGeneration,
    ) -> Result<Self, ReactiveRuntimeError> {
        let routes = generation
            .routes()
            .map_err(|error| ReactiveRuntimeError::new(error.to_string()))?;
        let dispatch = Dispatch::open(database_path).map_err(|error| {
            ReactiveRuntimeError::new(format!("cannot open native dispatch: {error}"))
        })?;
        let (sender, receiver) = mpsc::channel();
        let (wake, _initial_receiver) = RuntimeWake::new();
        let actor_wake = wake.clone();
        let thread = thread::Builder::new()
            .name("zeta-dispatch".to_owned())
            .spawn(move || run_actor(dispatch, routes, receiver, actor_wake))
            .map_err(|error| {
                ReactiveRuntimeError::new(format!("cannot start dispatch actor: {error}"))
            })?;
        Ok(Self {
            sender,
            wake,
            thread: Mutex::new(Some(thread)),
        })
    }

    /// Returns a receiver for every post-commit work notification.
    pub fn subscribe(&self) -> watch::Receiver<u64> {
        self.wake.subscribe()
    }

    /// Durably stores and immediately routes one external event.
    pub fn ingest(&self, draft: DraftEvent) -> Result<IngressResult, ReactiveRuntimeError> {
        self.request(|reply| RuntimeCommand::Ingest { draft, reply })
    }

    /// Replaces routes for future ingress without changing existing work.
    pub fn reload(&self, generation: ProjectGeneration) -> Result<(), ReactiveRuntimeError> {
        let routes = generation
            .routes()
            .map_err(|error| ReactiveRuntimeError::new(error.to_string()))?;
        self.request(|reply| RuntimeCommand::Reload { routes, reply })
    }

    /// Returns durable queue and timer state.
    pub fn status(&self) -> Result<ReactiveRuntimeStatus, ReactiveRuntimeError> {
        self.request(|reply| RuntimeCommand::Status { reply })
    }

    /// Stops the actor after its current durable command completes.
    pub fn shutdown(&self) -> Result<(), ReactiveRuntimeError> {
        let result = self.request(|reply| RuntimeCommand::Shutdown { reply });
        let thread = self
            .thread
            .lock()
            .map_err(|_error| ReactiveRuntimeError::new("the dispatch actor state is unavailable"))?
            .take();
        if let Some(thread) = thread {
            thread
                .join()
                .map_err(|_error| ReactiveRuntimeError::new("the dispatch actor panicked"))?;
        }
        result
    }

    fn request<T>(
        &self,
        build: impl FnOnce(mpsc::Sender<Result<T, ReactiveRuntimeError>>) -> RuntimeCommand,
    ) -> Result<T, ReactiveRuntimeError> {
        let (sender, receiver) = mpsc::channel();
        self.sender
            .send(build(sender))
            .map_err(|_error| ReactiveRuntimeError::new("the dispatch actor is not running"))?;
        receiver.recv().map_err(|_error| {
            ReactiveRuntimeError::new("the dispatch actor stopped before replying")
        })?
    }
}

impl Drop for ReactiveRuntime {
    fn drop(&mut self) {
        let _result = self.shutdown();
    }
}

enum RuntimeCommand {
    Ingest {
        draft: DraftEvent,
        reply: mpsc::Sender<Result<IngressResult, ReactiveRuntimeError>>,
    },
    Reload {
        routes: Vec<Route>,
        reply: mpsc::Sender<Result<(), ReactiveRuntimeError>>,
    },
    Status {
        reply: mpsc::Sender<Result<ReactiveRuntimeStatus, ReactiveRuntimeError>>,
    },
    Shutdown {
        reply: mpsc::Sender<Result<(), ReactiveRuntimeError>>,
    },
}

fn run_actor(
    mut dispatch: Dispatch,
    mut routes: Vec<Route>,
    receiver: mpsc::Receiver<RuntimeCommand>,
    wake: RuntimeWake,
) {
    if advance_actor_state(&mut dispatch, &routes).unwrap_or(false) {
        wake.signal();
    }
    loop {
        let deadline = match current_time_ms().and_then(|now| {
            if dispatch
                .has_due_maintenance(now)
                .map_err(|error| ReactiveRuntimeError::new(error.to_string()))?
            {
                return Ok(Some(now));
            }
            dispatch
                .next_deadline_ms(now)
                .map_err(|error| ReactiveRuntimeError::new(error.to_string()))
        }) {
            Ok(deadline) => deadline,
            Err(_error) => None,
        };
        let received = match deadline {
            Some(deadline) => receive_until(&receiver, deadline),
            None => receiver
                .recv()
                .map(Received::Command)
                .unwrap_or(Received::Closed),
        };
        match received {
            Received::Command(RuntimeCommand::Ingest { draft, reply }) => {
                let result = ingest_and_route(&mut dispatch, &routes, draft);
                if result.is_ok() {
                    wake.signal();
                }
                let _sent = reply.send(result);
            }
            Received::Command(RuntimeCommand::Reload {
                routes: replacement,
                reply,
            }) => {
                routes = replacement;
                let result = advance_actor_state(&mut dispatch, &routes).map(|_changed| ());
                if result.is_ok() {
                    wake.signal();
                }
                let _sent = reply.send(result);
            }
            Received::Command(RuntimeCommand::Status { reply }) => {
                let _sent = reply.send(status(&dispatch, &wake));
            }
            Received::Command(RuntimeCommand::Shutdown { reply }) => {
                let _sent = reply.send(Ok(()));
                return;
            }
            Received::Deadline => {
                if advance_actor_state(&mut dispatch, &routes).unwrap_or(false) {
                    wake.signal();
                }
            }
            Received::Closed => return,
        }
    }
}

enum Received {
    Command(RuntimeCommand),
    Deadline,
    Closed,
}

fn receive_until(receiver: &mpsc::Receiver<RuntimeCommand>, deadline_ms: i64) -> Received {
    let Ok(now_ms) = current_time_ms() else {
        return Received::Deadline;
    };
    let remaining_ms = deadline_ms.saturating_sub(now_ms);
    if remaining_ms == 0 {
        return Received::Deadline;
    }
    match receiver.recv_timeout(Duration::from_millis(remaining_ms as u64)) {
        Ok(command) => Received::Command(command),
        Err(mpsc::RecvTimeoutError::Timeout) => Received::Deadline,
        Err(mpsc::RecvTimeoutError::Disconnected) => Received::Closed,
    }
}

fn ingest_and_route(
    dispatch: &mut Dispatch,
    routes: &[Route],
    draft: DraftEvent,
) -> Result<IngressResult, ReactiveRuntimeError> {
    let event = Event::from_draft(&next_event_id("evt"), current_time_ms()?, draft);
    let outcome = dispatch
        .ingest_event(event)
        .map_err(|error| ReactiveRuntimeError::new(error.to_string()))?;
    let _routed = route_unrouted(dispatch, routes)?;
    let route_count = dispatch
        .list_queue_items()
        .map_err(|error| ReactiveRuntimeError::new(error.to_string()))?
        .into_iter()
        .filter(|item| item.event_id() == outcome.event.id && !item.target_agent().is_empty())
        .count();
    Ok(IngressResult {
        event_id: outcome.event.id,
        inserted: outcome.inserted,
        route_count,
    })
}

fn advance_actor_state(
    dispatch: &mut Dispatch,
    routes: &[Route],
) -> Result<bool, ReactiveRuntimeError> {
    let now_ms = current_time_ms()?;
    let reconciled = dispatch
        .reconcile_expired_claims(now_ms)
        .map_err(|error| ReactiveRuntimeError::new(error.to_string()))?;
    let due = dispatch
        .advance_due(now_ms, DUE_ADVANCE_LIMIT, || Ok(runtime_identity(now_ms)))
        .map_err(|error| ReactiveRuntimeError::new(error.to_string()))?;
    let routed = route_unrouted(dispatch, routes)?;
    Ok(reconciled > 0 || !due.is_empty() || routed > 0)
}

fn route_unrouted(
    dispatch: &mut Dispatch,
    routes: &[Route],
) -> Result<usize, ReactiveRuntimeError> {
    let event_ids = dispatch
        .unrouted_ingress_events()
        .map_err(|error| ReactiveRuntimeError::new(error.to_string()))?;
    let mut route_count: usize = 0;
    for event_id in event_ids {
        let event = dispatch
            .get_event(&event_id)
            .map_err(|error| ReactiveRuntimeError::new(error.to_string()))?
            .ok_or_else(|| {
                ReactiveRuntimeError::new("an unrouted event is absent from the journal")
            })?;
        let decisions = route_event(&event, routes)
            .map_err(|error| ReactiveRuntimeError::new(error.to_string()))?;
        let identity_count = if decisions.len() > 1 {
            decisions.len() + 1
        } else {
            1
        };
        let mut identities = Vec::with_capacity(identity_count);
        for _ in 0..identity_count {
            identities.push(runtime_identity(current_time_ms()?));
        }
        let outcome = dispatch
            .route_ingress_event(&event_id, routes, &identities)
            .map_err(|error| ReactiveRuntimeError::new(error.to_string()))?;
        route_count = route_count.saturating_add(outcome.decisions().len());
    }
    Ok(route_count)
}

fn status(
    dispatch: &Dispatch,
    wake: &RuntimeWake,
) -> Result<ReactiveRuntimeStatus, ReactiveRuntimeError> {
    let now_ms = current_time_ms()?;
    let mut queue = BTreeMap::new();
    for item in dispatch
        .list_queue_items()
        .map_err(|error| ReactiveRuntimeError::new(error.to_string()))?
    {
        *queue.entry(item.status().to_string()).or_insert(0) += 1;
    }
    let active_waits = dispatch
        .list_waits()
        .map_err(|error| ReactiveRuntimeError::new(error.to_string()))?
        .into_iter()
        .filter(|wait| wait.status() == zeta_dispatch::WaitStatus::Active)
        .count();
    let pending_publications = dispatch
        .list_deferred_publications()
        .map_err(|error| ReactiveRuntimeError::new(error.to_string()))?
        .into_iter()
        .filter(|publication| {
            publication.status() == zeta_dispatch::DeferredPublicationStatus::Pending
        })
        .count();
    Ok(ReactiveRuntimeStatus {
        wake_epoch: wake.epoch(),
        queue,
        active_waits,
        pending_publications,
        next_deadline_ms: dispatch
            .next_deadline_ms(now_ms)
            .map_err(|error| ReactiveRuntimeError::new(error.to_string()))?,
    })
}

fn current_time_ms() -> Result<i64, ReactiveRuntimeError> {
    let duration = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|error| ReactiveRuntimeError::new(error.to_string()))?;
    duration.as_millis().try_into().map_err(|_error| {
        ReactiveRuntimeError::new("the current time does not fit Unix milliseconds")
    })
}

fn next_event_id(prefix: &str) -> String {
    format!("{prefix}_{}", Uuid::new_v4())
}

fn runtime_identity(now_ms: i64) -> RuntimeEventIdentity {
    RuntimeEventIdentity::new(next_event_id("runtime"), now_ms)
        .expect("generated runtime identities are non-empty")
}

#[cfg(test)]
mod tests {
    use std::fs;

    use serde_json::Map;
    use tempfile::TempDir;

    use super::*;

    fn project(root: &Path) -> ProjectGeneration {
        let agents = root.join("agents");
        fs::create_dir_all(&agents).expect("agents directory");
        fs::write(
            agents.join("worker.md"),
            "---\nname: Worker\ndescription: Routes events.\naccepts: [example.created]\n---\nWork.\n",
        )
        .expect("agent source");
        ProjectGeneration::load(root).expect("project generation")
    }

    #[test]
    fn ingress_wakes_and_routes_without_a_poll_interval() {
        let temporary = TempDir::new().expect("temporary directory");
        let runtime = ReactiveRuntime::start(
            temporary.path().join("zeta.sqlite3"),
            project(temporary.path()),
        )
        .expect("reactive runtime");
        let wake = runtime.subscribe();
        let result = runtime
            .ingest(DraftEvent {
                event_type: "example.created".to_owned(),
                source: "test".to_owned(),
                payload: Map::new(),
                idempotency_key: Some("example:1".to_owned()),
                caused_by: None,
                session_id: None,
                run_id: None,
                turn_id: None,
            })
            .expect("ingress");
        assert!(result.inserted);
        assert_eq!(result.route_count, 1);
        assert!(wake.has_changed().expect("ingress wake"));
        let status = runtime.status().expect("runtime status");
        assert_eq!(status.queue.get("available"), Some(&1));
        runtime.shutdown().expect("runtime shutdown");
    }
}
