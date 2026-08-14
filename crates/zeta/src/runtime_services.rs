//! Concrete runtime services for native invocation composition.

use std::collections::BTreeSet;
use std::fmt;
use std::fs::{self, File};
use std::io::{self, Read, Seek, SeekFrom, Write};
use std::os::unix::fs::{FileTypeExt, PermissionsExt};
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::str::FromStr;
use std::sync::{Arc, Mutex};
use std::time::{SystemTime, UNIX_EPOCH};

use chrono::{DateTime, LocalResult, SecondsFormat, TimeZone, Timelike, Utc};
use chrono_tz::Tz;
use croner::Cron;
use rustix::fs::{FlockOperation, Mode, OFlags, flock, open};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use uuid::Uuid;
use zeta_agent::{
    AbortReason, AbortSignal, AgentError, AgentInvocation, AgentObserver, Capability, Clock,
    DeliverySemantics as AgentDeliverySemantics, DraftRecorder, IdSource, Observation,
    PromptEnvironment, PromptTransform, ResolvedCapability, ToolProfile,
};
use zeta_dispatch::{Dispatch, DispatchError};
use zeta_journal::{DraftEvent, Event, EventFilter};
use zeta_manifest::{
    AgentSpec, DeliverySemantics as AuthoredDeliverySemantics, ExecutionManifest,
    ExecutionManifestId, ImplementationFingerprint, ProjectManifest, ProjectRevisionId,
    ScheduleEntry, scheduled_event_type, verify_execution_manifest, verify_project_manifest,
};

const SCHEDULER_SOURCE: &str = "zeta:scheduler";
const SCHEDULER_TICK_PREFIX: &str = "zeta.scheduler.tick.";
const RUNTIME_METADATA_SCHEMA_VERSION: u64 = 1;
const RUNTIME_LOCK_NAME: &str = "runtime.lock";
const RUNTIME_METADATA_NAME: &str = "runtime.json";
const RUNTIME_METADATA_TEMP_NAME: &str = "runtime.json.tmp";
const RUNTIME_SOCKET_NAME: &str = "runtime.sock";
const RUNTIME_CONTROL_SOCKET_NAME: &str = "runtime-control.sock";
const RUNTIME_LOG_NAME: &str = "runtime.log";
const ACTIVE_PROJECT_NAME: &str = "active-project.json";
const DISPATCH_DATABASE_NAME: &str = "zeta.sqlite3";

/// Names the process mode recorded by one runtime owner.
///
/// # Examples
///
/// ```
/// let mode = zeta::runtime_services::RuntimeOwnerMode::Detached;
/// assert_eq!(serde_json::to_value(mode)?, "detached");
/// # Ok::<(), serde_json::Error>(())
/// ```
#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum RuntimeOwnerMode {
    /// Keeps the runtime attached to the invoking terminal.
    Foreground,
    /// Runs the runtime independently after a readiness handshake.
    Detached,
}

/// Names the lifecycle phase recorded by one runtime owner.
///
/// # Examples
///
/// ```
/// let phase = zeta::runtime_services::RuntimePhase::Running;
/// assert_eq!(serde_json::to_value(phase)?, "running");
/// # Ok::<(), serde_json::Error>(())
/// ```
#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum RuntimePhase {
    /// Accepts lifecycle and application traffic.
    Running,
    /// Rejects new ownership while resources shut down.
    Stopping,
}

/// Contains every project-local path owned by the runtime lifecycle.
///
/// # Examples
///
/// ```
/// let paths = zeta::runtime_services::RuntimePaths::new("/tmp/project/.zeta");
/// assert_eq!(paths.socket(), std::path::Path::new("/tmp/project/.zeta/runtime.sock"));
/// ```
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RuntimePaths {
    state_dir: PathBuf,
    lock: PathBuf,
    metadata: PathBuf,
    socket: PathBuf,
    control_socket: PathBuf,
    log: PathBuf,
    active_project: PathBuf,
    dispatch: PathBuf,
}

impl RuntimePaths {
    /// Derives lifecycle entries beneath one resolved state directory.
    pub fn new(state_dir: impl AsRef<Path>) -> Self {
        let state_dir = state_dir.as_ref().to_path_buf();
        Self {
            lock: state_dir.join(RUNTIME_LOCK_NAME),
            metadata: state_dir.join(RUNTIME_METADATA_NAME),
            socket: state_dir.join(RUNTIME_SOCKET_NAME),
            control_socket: state_dir.join(RUNTIME_CONTROL_SOCKET_NAME),
            log: state_dir.join(RUNTIME_LOG_NAME),
            active_project: state_dir.join(ACTIVE_PROJECT_NAME),
            dispatch: state_dir.join(DISPATCH_DATABASE_NAME),
            state_dir,
        }
    }

    /// Returns the directory containing native runtime state.
    pub fn state_dir(&self) -> &Path {
        &self.state_dir
    }

    /// Returns the persistent advisory-lock path.
    pub fn lock(&self) -> &Path {
        &self.lock
    }

    /// Returns the atomically replaced owner-metadata path.
    pub fn metadata(&self) -> &Path {
        &self.metadata
    }

    /// Returns the ordinary application-client socket path.
    pub fn socket(&self) -> &Path {
        &self.socket
    }

    /// Returns the owner-authorized control socket path.
    pub fn control_socket(&self) -> &Path {
        &self.control_socket
    }

    /// Returns the detached-runtime diagnostics path.
    pub fn log(&self) -> &Path {
        &self.log
    }

    /// Returns the atomically replaced active project revision document.
    pub fn active_project(&self) -> &Path {
        &self.active_project
    }

    /// Returns the durable native Dispatch database path.
    pub fn dispatch(&self) -> &Path {
        &self.dispatch
    }
}

/// Records the identity and filesystem contract of one runtime owner.
///
/// # Examples
///
/// ```
/// let paths = zeta::runtime_services::RuntimePaths::new("/tmp/project/.zeta");
/// let metadata = zeta::runtime_services::RuntimeMetadata::new(
///     "instance-1",
///     42,
///     zeta::runtime_services::RuntimeOwnerMode::Foreground,
///     zeta::runtime_services::RuntimePhase::Running,
///     "/tmp/project",
///     &paths,
///     1,
/// );
/// assert_eq!(metadata.instance_id, "instance-1");
/// ```
#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub struct RuntimeMetadata {
    /// Identifies the metadata document schema.
    pub schema_version: u64,
    /// Identifies the native Zeta build.
    pub zeta_version: String,
    /// Distinguishes this ownership lifetime from every prior process.
    pub instance_id: String,
    /// Records the owner process for inspection only.
    pub pid: u32,
    /// Records whether the owner is attached or detached.
    pub mode: RuntimeOwnerMode,
    /// Records whether the owner is running or stopping.
    pub phase: RuntimePhase,
    /// Contains the resolved authored project root.
    pub project_root: PathBuf,
    /// Contains the resolved project state directory.
    pub state_dir: PathBuf,
    /// Contains the ordinary application socket path.
    pub socket: PathBuf,
    /// Contains the process-authority socket path.
    pub control_socket: PathBuf,
    /// Records runtime creation in Unix milliseconds.
    pub started_at_ms: i64,
}

impl RuntimeMetadata {
    /// Creates one versioned owner document from resolved lifecycle paths.
    pub fn new(
        instance_id: impl Into<String>,
        pid: u32,
        mode: RuntimeOwnerMode,
        phase: RuntimePhase,
        project_root: impl AsRef<Path>,
        paths: &RuntimePaths,
        started_at_ms: i64,
    ) -> Self {
        Self {
            schema_version: RUNTIME_METADATA_SCHEMA_VERSION,
            zeta_version: env!("CARGO_PKG_VERSION").to_owned(),
            instance_id: instance_id.into(),
            pid,
            mode,
            phase,
            project_root: project_root.as_ref().to_path_buf(),
            state_dir: paths.state_dir.clone(),
            socket: paths.socket.clone(),
            control_socket: paths.control_socket.clone(),
            started_at_ms,
        }
    }

    /// Reads the current owner metadata without creating lifecycle state.
    ///
    /// # Errors
    ///
    /// Returns [`RuntimeLifecycleError`] when metadata cannot be read or does
    /// not contain one complete owner document.
    pub fn read(paths: &RuntimePaths) -> Result<Option<Self>, RuntimeLifecycleError> {
        let file = match open(
            paths.metadata(),
            OFlags::RDONLY | OFlags::CLOEXEC | OFlags::NOFOLLOW,
            Mode::empty(),
        ) {
            Ok(file) => file,
            Err(error) => {
                if error == rustix::io::Errno::NOENT {
                    return Ok(None);
                }
                return Err(RuntimeLifecycleError::new(
                    RuntimeLifecycleErrorKind::Metadata,
                    paths.metadata(),
                    error.to_string(),
                ));
            }
        };
        let mut bytes = Vec::new();
        File::from(file).read_to_end(&mut bytes).map_err(|error| {
            RuntimeLifecycleError::new(
                RuntimeLifecycleErrorKind::Metadata,
                paths.metadata(),
                error.to_string(),
            )
        })?;
        serde_json::from_slice(&bytes).map(Some).map_err(|error| {
            RuntimeLifecycleError::new(
                RuntimeLifecycleErrorKind::Metadata,
                paths.metadata(),
                error.to_string(),
            )
        })
    }
}

/// Classifies one native runtime lifecycle failure.
///
/// # Examples
///
/// ```
/// let kind = zeta::runtime_services::RuntimeLifecycleErrorKind::LeaseHeld;
/// assert_eq!(kind.reason(), "lease_held");
/// ```
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum RuntimeLifecycleErrorKind {
    /// The state directory cannot be created or inspected safely.
    StateDirectory,
    /// The persistent lease file cannot be opened or locked.
    Lease,
    /// Another process currently owns the lifecycle lease.
    LeaseHeld,
    /// Owner metadata cannot be encoded, committed, or read.
    Metadata,
    /// A lifecycle socket path contains a non-socket entry.
    SocketOccupied,
    /// A lifecycle socket accepts connections without this lease owner.
    SocketLive,
    /// A lifecycle socket cannot be inspected safely.
    SocketInspect,
    /// A proven stale lifecycle socket cannot be removed.
    SocketCleanup,
}

impl RuntimeLifecycleErrorKind {
    /// Returns the stable machine-readable failure reason.
    pub fn reason(self) -> &'static str {
        match self {
            RuntimeLifecycleErrorKind::StateDirectory => "state_directory",
            RuntimeLifecycleErrorKind::Lease => "lease",
            RuntimeLifecycleErrorKind::LeaseHeld => "lease_held",
            RuntimeLifecycleErrorKind::Metadata => "metadata",
            RuntimeLifecycleErrorKind::SocketOccupied => "socket_occupied",
            RuntimeLifecycleErrorKind::SocketLive => "socket_live",
            RuntimeLifecycleErrorKind::SocketInspect => "socket_inspect",
            RuntimeLifecycleErrorKind::SocketCleanup => "socket_cleanup",
        }
    }
}

/// Reports why a runtime lease, metadata, or stale socket operation failed.
#[derive(Debug)]
pub struct RuntimeLifecycleError {
    kind: RuntimeLifecycleErrorKind,
    path: PathBuf,
    detail: String,
}

impl RuntimeLifecycleError {
    fn new(kind: RuntimeLifecycleErrorKind, path: &Path, detail: impl Into<String>) -> Self {
        Self {
            kind,
            path: path.to_path_buf(),
            detail: detail.into(),
        }
    }

    /// Returns the structured lifecycle failure class.
    pub fn kind(&self) -> RuntimeLifecycleErrorKind {
        self.kind
    }

    /// Returns the stable machine-readable failure reason.
    pub fn reason(&self) -> &'static str {
        self.kind.reason()
    }
}

impl fmt::Display for RuntimeLifecycleError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            formatter,
            "{} at '{}': {}",
            self.reason(),
            self.path.display(),
            self.detail
        )
    }
}

impl std::error::Error for RuntimeLifecycleError {}

/// Reports how one lifecycle socket changed during stale reconciliation.
///
/// # Examples
///
/// ```
/// let disposition = zeta::runtime_services::RuntimeSocketDisposition::Missing;
/// assert_eq!(disposition, zeta::runtime_services::RuntimeSocketDisposition::Missing);
/// ```
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum RuntimeSocketDisposition {
    /// No filesystem entry existed.
    Missing,
    /// A Unix socket that refused connections was removed.
    RemovedStale,
}

/// Reports stale reconciliation for both lifecycle endpoints.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct RuntimeSocketReconciliation {
    /// Reports the ordinary application socket disposition.
    pub runtime: RuntimeSocketDisposition,
    /// Reports the process-authority socket disposition.
    pub control: RuntimeSocketDisposition,
}

/// Owns the exclusive advisory lease for one project runtime.
///
/// Dropping the value releases the advisory lock but preserves its inode for
/// the next owner.
#[derive(Debug)]
pub struct RuntimeLease {
    paths: RuntimePaths,
    _lock: File,
}

impl RuntimeLease {
    /// Creates owner-only state and acquires its nonblocking exclusive lease.
    ///
    /// # Errors
    ///
    /// Returns [`RuntimeLifecycleError`] when state cannot be prepared, the
    /// lease entry is unsafe, or another owner holds its advisory lock.
    pub fn acquire(paths: RuntimePaths) -> Result<Self, RuntimeLifecycleError> {
        prepare_runtime_state(paths.state_dir())?;
        let flags = OFlags::RDWR | OFlags::CREATE | OFlags::CLOEXEC | OFlags::NOFOLLOW;
        let mode = Mode::RUSR | Mode::WUSR;
        let lock = open(paths.lock(), flags, mode).map_err(|error| {
            RuntimeLifecycleError::new(
                RuntimeLifecycleErrorKind::Lease,
                paths.lock(),
                error.to_string(),
            )
        })?;
        let lock = File::from(lock);
        let result = flock(&lock, FlockOperation::NonBlockingLockExclusive);
        let Ok(()) = result else {
            let error = result.expect_err("the let-else observed a lease error");
            let kind = if error == rustix::io::Errno::WOULDBLOCK {
                RuntimeLifecycleErrorKind::LeaseHeld
            } else {
                RuntimeLifecycleErrorKind::Lease
            };
            return Err(RuntimeLifecycleError::new(
                kind,
                paths.lock(),
                error.to_string(),
            ));
        };
        lock.set_permissions(fs::Permissions::from_mode(0o600))
            .map_err(|error| {
                RuntimeLifecycleError::new(
                    RuntimeLifecycleErrorKind::Lease,
                    paths.lock(),
                    error.to_string(),
                )
            })?;
        Ok(Self { paths, _lock: lock })
    }

    /// Atomically replaces owner metadata through an owner-only sibling file.
    ///
    /// # Errors
    ///
    /// Returns [`RuntimeLifecycleError`] when the document does not describe
    /// these lifecycle paths or cannot be durably replaced.
    pub fn write_metadata(&self, metadata: &RuntimeMetadata) -> Result<(), RuntimeLifecycleError> {
        if metadata.state_dir != self.paths.state_dir
            || metadata.socket != self.paths.socket
            || metadata.control_socket != self.paths.control_socket
        {
            return Err(RuntimeLifecycleError::new(
                RuntimeLifecycleErrorKind::Metadata,
                self.paths.metadata(),
                "owner metadata does not match the held lifecycle paths",
            ));
        }
        write_runtime_metadata(&self.paths, metadata)
    }

    /// Removes only lifecycle sockets proven stale while this lease is held.
    ///
    /// Both paths are inspected before either stale entry is removed. A live
    /// socket, regular file, symlink, or ambiguous connection error preserves
    /// both entries.
    ///
    /// # Errors
    ///
    /// Returns [`RuntimeLifecycleError`] when either entry is occupied, live,
    /// cannot be inspected, or cannot be removed.
    pub fn reconcile_stale_sockets(
        &self,
    ) -> Result<RuntimeSocketReconciliation, RuntimeLifecycleError> {
        let runtime = inspect_runtime_socket(self.paths.socket())?;
        let control = inspect_runtime_socket(self.paths.control_socket())?;
        let runtime = reconcile_runtime_socket(self.paths.socket(), runtime)?;
        let control = reconcile_runtime_socket(self.paths.control_socket(), control)?;
        Ok(RuntimeSocketReconciliation { runtime, control })
    }

    /// Removes metadata only when it still names this ownership lifetime.
    ///
    /// # Errors
    ///
    /// Returns [`RuntimeLifecycleError`] when metadata is unsafe, unreadable,
    /// or cannot be removed. A missing or replaced document is preserved.
    pub fn remove_metadata(&self, instance_id: &str) -> Result<bool, RuntimeLifecycleError> {
        let Some(metadata) = RuntimeMetadata::read(&self.paths)? else {
            return Ok(false);
        };
        if metadata.instance_id != instance_id {
            return Ok(false);
        }
        fs::remove_file(self.paths.metadata()).map_err(|error| {
            RuntimeLifecycleError::new(
                RuntimeLifecycleErrorKind::Metadata,
                self.paths.metadata(),
                error.to_string(),
            )
        })?;
        Ok(true)
    }

    /// Removes an ownerless regular metadata entry while this lease is held.
    ///
    /// # Errors
    ///
    /// Returns [`RuntimeLifecycleError`] when the entry is not a regular file,
    /// cannot be inspected, or cannot be removed.
    pub fn remove_stale_metadata(&self) -> Result<bool, RuntimeLifecycleError> {
        let metadata = match fs::symlink_metadata(self.paths.metadata()) {
            Ok(metadata) => metadata,
            Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(false),
            Err(error) => {
                return Err(RuntimeLifecycleError::new(
                    RuntimeLifecycleErrorKind::Metadata,
                    self.paths.metadata(),
                    error.to_string(),
                ));
            }
        };
        if !metadata.file_type().is_file() {
            return Err(RuntimeLifecycleError::new(
                RuntimeLifecycleErrorKind::Metadata,
                self.paths.metadata(),
                "stale metadata is not a regular file",
            ));
        }
        fs::remove_file(self.paths.metadata()).map_err(|error| {
            RuntimeLifecycleError::new(
                RuntimeLifecycleErrorKind::Metadata,
                self.paths.metadata(),
                error.to_string(),
            )
        })?;
        Ok(true)
    }
}

fn prepare_runtime_state(state_dir: &Path) -> Result<(), RuntimeLifecycleError> {
    match fs::symlink_metadata(state_dir) {
        Ok(metadata) => {
            if metadata.is_dir() {
                return Ok(());
            }
            if metadata.file_type().is_symlink() {
                let target = fs::metadata(state_dir).map_err(|error| {
                    RuntimeLifecycleError::new(
                        RuntimeLifecycleErrorKind::StateDirectory,
                        state_dir,
                        error.to_string(),
                    )
                })?;
                if target.is_dir() {
                    return Ok(());
                }
            }
            Err(RuntimeLifecycleError::new(
                RuntimeLifecycleErrorKind::StateDirectory,
                state_dir,
                "the runtime state path is not a directory",
            ))
        }
        Err(error) => {
            if error.kind() != io::ErrorKind::NotFound {
                return Err(RuntimeLifecycleError::new(
                    RuntimeLifecycleErrorKind::StateDirectory,
                    state_dir,
                    error.to_string(),
                ));
            }
            fs::create_dir_all(state_dir).map_err(|error| {
                RuntimeLifecycleError::new(
                    RuntimeLifecycleErrorKind::StateDirectory,
                    state_dir,
                    error.to_string(),
                )
            })?;
            fs::set_permissions(state_dir, fs::Permissions::from_mode(0o700)).map_err(|error| {
                RuntimeLifecycleError::new(
                    RuntimeLifecycleErrorKind::StateDirectory,
                    state_dir,
                    error.to_string(),
                )
            })
        }
    }
}

fn write_runtime_metadata(
    paths: &RuntimePaths,
    metadata: &RuntimeMetadata,
) -> Result<(), RuntimeLifecycleError> {
    let temporary = paths.state_dir().join(RUNTIME_METADATA_TEMP_NAME);
    let flags =
        OFlags::WRONLY | OFlags::CREATE | OFlags::TRUNC | OFlags::CLOEXEC | OFlags::NOFOLLOW;
    let mode = Mode::RUSR | Mode::WUSR;
    let file = open(&temporary, flags, mode).map_err(|error| {
        RuntimeLifecycleError::new(
            RuntimeLifecycleErrorKind::Metadata,
            &temporary,
            error.to_string(),
        )
    })?;
    let mut file = File::from(file);
    let result = write_runtime_metadata_file(&temporary, &mut file, metadata);
    if let Err(error) = result {
        let _cleanup = fs::remove_file(&temporary);
        return Err(error);
    }
    drop(file);
    let result = fs::rename(&temporary, paths.metadata());
    let Ok(()) = result else {
        let error = result.expect_err("the let-else observed a metadata rename error");
        let _cleanup = fs::remove_file(&temporary);
        return Err(RuntimeLifecycleError::new(
            RuntimeLifecycleErrorKind::Metadata,
            paths.metadata(),
            error.to_string(),
        ));
    };
    let directory = File::open(paths.state_dir()).map_err(|error| {
        RuntimeLifecycleError::new(
            RuntimeLifecycleErrorKind::Metadata,
            paths.state_dir(),
            error.to_string(),
        )
    })?;
    directory.sync_all().map_err(|error| {
        RuntimeLifecycleError::new(
            RuntimeLifecycleErrorKind::Metadata,
            paths.state_dir(),
            error.to_string(),
        )
    })
}

fn write_runtime_metadata_file(
    path: &Path,
    file: &mut File,
    metadata: &RuntimeMetadata,
) -> Result<(), RuntimeLifecycleError> {
    file.set_permissions(fs::Permissions::from_mode(0o600))
        .map_err(|error| {
            RuntimeLifecycleError::new(RuntimeLifecycleErrorKind::Metadata, path, error.to_string())
        })?;
    file.seek(SeekFrom::Start(0)).map_err(|error| {
        RuntimeLifecycleError::new(RuntimeLifecycleErrorKind::Metadata, path, error.to_string())
    })?;
    serde_json::to_writer(&mut *file, metadata).map_err(|error| {
        RuntimeLifecycleError::new(RuntimeLifecycleErrorKind::Metadata, path, error.to_string())
    })?;
    file.write_all(b"\n").map_err(|error| {
        RuntimeLifecycleError::new(RuntimeLifecycleErrorKind::Metadata, path, error.to_string())
    })?;
    file.sync_all().map_err(|error| {
        RuntimeLifecycleError::new(RuntimeLifecycleErrorKind::Metadata, path, error.to_string())
    })
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum RuntimeSocketInspection {
    Missing,
    Stale,
}

fn inspect_runtime_socket(path: &Path) -> Result<RuntimeSocketInspection, RuntimeLifecycleError> {
    let metadata = match fs::symlink_metadata(path) {
        Ok(metadata) => metadata,
        Err(error) => {
            if error.kind() == io::ErrorKind::NotFound {
                return Ok(RuntimeSocketInspection::Missing);
            }
            return Err(RuntimeLifecycleError::new(
                RuntimeLifecycleErrorKind::SocketInspect,
                path,
                error.to_string(),
            ));
        }
    };
    if !metadata.file_type().is_socket() {
        return Err(RuntimeLifecycleError::new(
            RuntimeLifecycleErrorKind::SocketOccupied,
            path,
            "the lifecycle socket path contains a non-socket entry",
        ));
    }
    match UnixStream::connect(path) {
        Ok(stream) => {
            drop(stream);
            Err(RuntimeLifecycleError::new(
                RuntimeLifecycleErrorKind::SocketLive,
                path,
                "the lifecycle socket accepts connections",
            ))
        }
        Err(error) => {
            let kind = error.kind();
            if kind == io::ErrorKind::ConnectionRefused {
                return Ok(RuntimeSocketInspection::Stale);
            }
            if kind == io::ErrorKind::NotFound {
                return Ok(RuntimeSocketInspection::Missing);
            }
            Err(RuntimeLifecycleError::new(
                RuntimeLifecycleErrorKind::SocketInspect,
                path,
                error.to_string(),
            ))
        }
    }
}

fn reconcile_runtime_socket(
    path: &Path,
    inspection: RuntimeSocketInspection,
) -> Result<RuntimeSocketDisposition, RuntimeLifecycleError> {
    match inspection {
        RuntimeSocketInspection::Missing => Ok(RuntimeSocketDisposition::Missing),
        RuntimeSocketInspection::Stale => {
            fs::remove_file(path).map_err(|error| {
                RuntimeLifecycleError::new(
                    RuntimeLifecycleErrorKind::SocketCleanup,
                    path,
                    error.to_string(),
                )
            })?;
            Ok(RuntimeSocketDisposition::RemovedStale)
        }
    }
}

/// Classifies a native scheduler failure.
///
/// # Examples
///
/// ```
/// assert_eq!(zeta::SchedulerErrorKind::InvalidCron.reason(), "invalid_cron");
/// ```
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum SchedulerErrorKind {
    /// A project manifest fails its canonical verification.
    InvalidManifest,
    /// A cron declaration is malformed or outside the five-field contract.
    InvalidCron,
    /// A schedule names an unknown IANA timezone.
    UnknownTimezone,
    /// A valid calendar declaration cannot produce an occurrence.
    Calendar,
    /// A host identity source cannot supply an event id.
    Identity,
    /// Dispatch cannot read or append scheduler state.
    Persistence,
}

impl SchedulerErrorKind {
    /// Returns a stable machine-readable reason.
    ///
    /// # Examples
    ///
    /// ```
    /// assert_eq!(
    ///     zeta::SchedulerErrorKind::UnknownTimezone.reason(),
    ///     "unknown_timezone",
    /// );
    /// ```
    pub fn reason(self) -> &'static str {
        match self {
            SchedulerErrorKind::InvalidManifest => "invalid_manifest",
            SchedulerErrorKind::InvalidCron => "invalid_cron",
            SchedulerErrorKind::UnknownTimezone => "unknown_timezone",
            SchedulerErrorKind::Calendar => "calendar",
            SchedulerErrorKind::Identity => "identity",
            SchedulerErrorKind::Persistence => "persistence",
        }
    }
}

/// Reports why a native schedule cannot compile, tick, or project status.
///
/// # Examples
///
/// ```
/// # fn inspect(error: &zeta::SchedulerError) {
/// assert!(!error.detail().is_empty());
/// let _kind = error.kind();
/// # }
/// ```
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct SchedulerError {
    kind: SchedulerErrorKind,
    detail: String,
}

impl SchedulerError {
    fn new(kind: SchedulerErrorKind, detail: impl Into<String>) -> Self {
        SchedulerError {
            kind,
            detail: detail.into(),
        }
    }

    /// Returns the stable failure class.
    ///
    /// # Examples
    ///
    /// ```
    /// # fn inspect(error: &zeta::SchedulerError) {
    /// let _kind: zeta::SchedulerErrorKind = error.kind();
    /// # }
    /// ```
    pub fn kind(&self) -> SchedulerErrorKind {
        self.kind
    }

    /// Returns the stable machine-readable reason.
    ///
    /// # Examples
    ///
    /// ```
    /// # fn inspect(error: &zeta::SchedulerError) {
    /// assert_eq!(error.reason(), error.kind().reason());
    /// # }
    /// ```
    pub fn reason(&self) -> &'static str {
        self.kind.reason()
    }

    /// Returns the human-readable failure detail.
    ///
    /// # Examples
    ///
    /// ```
    /// # fn inspect(error: &zeta::SchedulerError) {
    /// assert!(!error.detail().is_empty());
    /// # }
    /// ```
    pub fn detail(&self) -> &str {
        &self.detail
    }
}

impl fmt::Display for SchedulerError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "{}: {}", self.reason(), self.detail)
    }
}

impl std::error::Error for SchedulerError {}

/// Describes one authored schedule's journal-derived runtime status.
///
/// # Examples
///
/// ```
/// # fn inspect(status: &zeta::ScheduleStatus) {
/// assert!(!status.agent.is_empty());
/// assert!(!status.next_at.is_empty());
/// # }
/// ```
#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub struct ScheduleStatus {
    /// Names the owning agent.
    pub agent: String,
    /// Preserves the authored five-field cron expression.
    pub cron: String,
    /// Preserves the authored timezone, or `None` for the UTC default.
    pub timezone: Option<String>,
    /// Reports `pending`, `published`, `skipped`, or `missed`.
    pub status: String,
    /// Carries the most recent published intended occurrence.
    pub last_published_at: Option<String>,
    /// Carries the next intended occurrence after the observation time.
    pub next_at: String,
    /// Explains the current status.
    pub reason: String,
}

#[derive(Clone, Debug)]
struct CompiledSchedule {
    agent: String,
    index: u64,
    event_type: String,
    declaration: ScheduleEntry,
    cron: Cron,
    timezone: Tz,
}

/// Evaluates one verified project's recurring schedules against durable Dispatch state.
///
/// Construction compiles every enabled calendar declaration before a tick can
/// write any journal fact. Runtime state remains in Dispatch rather than this
/// immutable host value.
///
/// # Examples
///
/// ```
/// # fn compile(
/// #     manifest: &zeta_manifest::ProjectManifest,
/// # ) -> Result<(), zeta::SchedulerError> {
/// let scheduler = zeta::Scheduler::from_project(manifest)?;
/// let _ = scheduler;
/// # Ok(())
/// # }
/// ```
#[derive(Clone, Debug)]
pub struct Scheduler {
    schedules: Vec<CompiledSchedule>,
}

impl Scheduler {
    /// Compiles every enabled schedule in a verified project manifest.
    ///
    /// # Examples
    ///
    /// ```
    /// # fn compile(
    /// #     project: &zeta_manifest::ProjectManifest,
    /// # ) -> Result<(), zeta::SchedulerError> {
    /// let scheduler = zeta::Scheduler::from_project(project)?;
    /// let _ = scheduler;
    /// # Ok(())
    /// # }
    /// ```
    ///
    /// # Errors
    ///
    /// Returns [`SchedulerError`] when manifest verification fails, a cron is
    /// not exactly five fields, a cron is invalid, or a timezone is unknown.
    pub fn from_project(project: &ProjectManifest) -> Result<Self, SchedulerError> {
        verify_project_manifest(project).map_err(|error| {
            SchedulerError::new(SchedulerErrorKind::InvalidManifest, error.to_string())
        })?;
        Self::from_agents(project.agents.values())
    }

    /// Compiles direct agent declarations for the native Markdown host.
    ///
    /// The host retains these declarations in an immutable project revision.
    /// It has no authored project manifest file.
    pub(crate) fn from_agents<'a>(
        agents: impl IntoIterator<Item = &'a AgentSpec>,
    ) -> Result<Self, SchedulerError> {
        let mut schedules = Vec::new();
        for spec in agents {
            if !spec.enabled {
                continue;
            }
            for (index, declaration) in spec.schedules.iter().enumerate() {
                let index = u64::try_from(index).map_err(|error| {
                    SchedulerError::new(
                        SchedulerErrorKind::InvalidManifest,
                        format!(
                            "schedule index for agent {:?} is too large: {error}",
                            spec.slug
                        ),
                    )
                })?;
                let cron = compile_cron(&declaration.cron)?;
                let timezone = parse_timezone(declaration.timezone.as_deref())?;
                schedules.push(CompiledSchedule {
                    agent: spec.slug.clone(),
                    index,
                    event_type: scheduled_event_type(&spec.slug),
                    declaration: declaration.clone(),
                    cron,
                    timezone,
                });
            }
        }
        Ok(Scheduler { schedules })
    }

    /// Records every due occurrence and its scheduler audit facts.
    ///
    /// Only newly inserted ordinary occurrence events are returned. Retried
    /// ticks resolve retained events and repair missing decision facts through
    /// their stable idempotency keys.
    ///
    /// # Examples
    ///
    /// ```
    /// # fn tick(
    /// #     project: &zeta_manifest::ProjectManifest,
    /// #     dispatch: &mut zeta_dispatch::Dispatch,
    /// #     ids: &mut dyn zeta_agent::IdSource,
    /// # ) -> Result<(), zeta::SchedulerError> {
    /// let scheduler = zeta::Scheduler::from_project(project)?;
    /// let inserted = scheduler.tick(dispatch, 1_786_615_633_000, ids)?;
    /// for event in inserted {
    ///     assert_eq!(event.source, "zeta:scheduler");
    /// }
    /// # Ok(())
    /// # }
    /// ```
    ///
    /// # Errors
    ///
    /// Returns [`SchedulerError`] when time is out of range, a calendar search
    /// fails, the identity source fails, a retained scheduler fact is malformed,
    /// or Dispatch cannot read or append durable state.
    pub fn tick(
        &self,
        dispatch: &mut Dispatch,
        now_ms: i64,
        ids: &mut dyn IdSource,
    ) -> Result<Vec<Event>, SchedulerError> {
        let now = scheduler_now(now_ms)?;
        let mut requested = Vec::new();
        for schedule in &self.schedules {
            let activated_at = if schedule.declaration.catchup.as_deref() == Some("latest") {
                Some(activate_schedule(dispatch, schedule, now, now_ms, ids)?)
            } else {
                None
            };
            let previous = previous_schedule_time(schedule, now)?;
            let current = now.with_timezone(&schedule.timezone);
            let due = if previous.date_naive() == current.date_naive() {
                true
            } else {
                match activated_at {
                    Some(activated_at) => previous >= activated_at,
                    None => false,
                }
            };
            if !due {
                record_missed_schedule(dispatch, schedule, now, now_ms, previous, ids)?;
                continue;
            }
            let next = next_schedule_time(schedule, now)?;
            let occurrence_id = next_scheduler_id(ids)?;
            let occurrence =
                Event::from_draft(&occurrence_id, now_ms, occurrence_draft(schedule, previous));
            let outcome = dispatch
                .ingest_event(occurrence)
                .map_err(scheduler_persistence_error)?;
            let decisions = scheduler_events(dispatch)?;
            let missing_decision =
                !outcome.inserted && !schedule_tick_recorded(&decisions, schedule, previous);
            let published = outcome.inserted || missing_decision;
            let status = if published { "published" } else { "skipped" };
            let reason = if published {
                schedule_tick_reason(previous, current)
            } else {
                "already published"
            };
            let observed_at = format_schedule_time(current);
            let decision_id = next_scheduler_id(ids)?;
            let decision = Event::from_draft(
                &decision_id,
                now_ms,
                decision_draft(
                    schedule,
                    previous,
                    &observed_at,
                    next,
                    status,
                    reason,
                    Some(&outcome.event.id),
                ),
            );
            dispatch
                .append_trusted_event(decision)
                .map_err(scheduler_persistence_error)?;
            if outcome.inserted {
                requested.push(outcome.event);
            }
        }
        Ok(requested)
    }

    /// Derives current schedule status from cursor-ordered scheduler facts.
    ///
    /// # Examples
    ///
    /// ```
    /// # fn status(
    /// #     project: &zeta_manifest::ProjectManifest,
    /// #     dispatch: &zeta_dispatch::Dispatch,
    /// # ) -> Result<(), zeta::SchedulerError> {
    /// let scheduler = zeta::Scheduler::from_project(project)?;
    /// let rows = scheduler.status(dispatch, 1_786_615_633_000)?;
    /// for row in rows {
    ///     assert!(!row.next_at.is_empty());
    /// }
    /// # Ok(())
    /// # }
    /// ```
    ///
    /// # Errors
    ///
    /// Returns [`SchedulerError`] when time is out of range, calendar
    /// evaluation fails, or Dispatch cannot read the scheduler facts.
    pub fn status(
        &self,
        dispatch: &Dispatch,
        now_ms: i64,
    ) -> Result<Vec<ScheduleStatus>, SchedulerError> {
        let now = scheduler_now(now_ms)?;
        let decisions = scheduler_events(dispatch)?;
        let mut rows = Vec::new();
        for schedule in &self.schedules {
            let previous = previous_schedule_time(schedule, now)?;
            let previous = format_schedule_time(previous);
            let next = format_schedule_time(next_schedule_time(schedule, now)?);
            let mut matching = Vec::new();
            for event in &decisions {
                if schedule_decision_matches(event, schedule) {
                    matching.push(event);
                }
            }
            let mut latest_decision = None;
            let mut latest_published = None;
            for event in matching.iter().rev() {
                if latest_decision.is_none()
                    && event.payload.get("scheduled_at").and_then(Value::as_str)
                        == Some(previous.as_str())
                {
                    latest_decision = Some(*event);
                }
                if latest_published.is_none()
                    && event.payload.get("status").and_then(Value::as_str) == Some("published")
                {
                    latest_published = Some(*event);
                }
            }
            let (status, reason) = match latest_decision {
                Some(event) => (
                    required_scheduler_string(event, "status")?.to_owned(),
                    required_scheduler_string(event, "reason")?.to_owned(),
                ),
                None => (
                    "pending".to_owned(),
                    "next tick is in the future".to_owned(),
                ),
            };
            let last_published_at = match latest_published {
                Some(event) => Some(required_scheduler_string(event, "scheduled_at")?.to_owned()),
                None => None,
            };
            rows.push(ScheduleStatus {
                agent: schedule.agent.clone(),
                cron: schedule.declaration.cron.clone(),
                timezone: schedule.declaration.timezone.clone(),
                status,
                last_published_at,
                next_at: next,
                reason,
            });
        }
        Ok(rows)
    }
}

fn compile_cron(expression: &str) -> Result<Cron, SchedulerError> {
    let mut field_count = 0;
    for _field in expression.split_whitespace() {
        field_count += 1;
    }
    if field_count != 5 {
        return Err(SchedulerError::new(
            SchedulerErrorKind::InvalidCron,
            format!("unsupported cron expression {expression:?}"),
        ));
    }
    Cron::from_str(expression).map_err(|error| {
        SchedulerError::new(
            SchedulerErrorKind::InvalidCron,
            format!("invalid cron expression {expression:?}: {error}"),
        )
    })
}

fn parse_timezone(timezone: Option<&str>) -> Result<Tz, SchedulerError> {
    let Some(timezone) = timezone else {
        return Ok(chrono_tz::UTC);
    };
    Tz::from_str(timezone).map_err(|error| {
        SchedulerError::new(
            SchedulerErrorKind::UnknownTimezone,
            format!("unknown timezone {timezone:?}: {error}"),
        )
    })
}

fn scheduler_now(now_ms: i64) -> Result<DateTime<Utc>, SchedulerError> {
    let LocalResult::Single(now) = Utc.timestamp_millis_opt(now_ms) else {
        return Err(SchedulerError::new(
            SchedulerErrorKind::Calendar,
            format!("Unix millisecond time {now_ms} is out of range"),
        ));
    };
    Ok(now)
}

fn previous_schedule_time(
    schedule: &CompiledSchedule,
    now: DateTime<Utc>,
) -> Result<DateTime<Tz>, SchedulerError> {
    let base = now
        .with_second(59)
        .and_then(|current| current.with_nanosecond(999_999_999))
        .ok_or_else(|| {
            SchedulerError::new(
                SchedulerErrorKind::Calendar,
                format!(
                    "cannot align current time for cron {:?}",
                    schedule.declaration.cron
                ),
            )
        })?
        .with_timezone(&schedule.timezone);
    let previous = schedule
        .cron
        .find_previous_occurrence(&base, false)
        .map_err(|error| calendar_search_error(schedule, "previous", error))?;
    let next = schedule
        .cron
        .find_next_occurrence(&previous, false)
        .map_err(|error| calendar_search_error(schedule, "next", error))?;
    let mut selected = None;
    for candidate in [previous, next] {
        for candidate in physical_occurrences(schedule.timezone, candidate) {
            if candidate > base {
                continue;
            }
            match selected {
                Some(current) if current >= candidate => {}
                Some(_current) => selected = Some(candidate),
                None => selected = Some(candidate),
            }
        }
    }
    let Some(selected) = selected else {
        return Err(SchedulerError::new(
            SchedulerErrorKind::Calendar,
            format!(
                "cannot select previous occurrence for cron {:?}",
                schedule.declaration.cron
            ),
        ));
    };
    Ok(selected)
}

fn next_schedule_time(
    schedule: &CompiledSchedule,
    now: DateTime<Utc>,
) -> Result<DateTime<Tz>, SchedulerError> {
    let current = now.with_timezone(&schedule.timezone);
    let previous = schedule
        .cron
        .find_previous_occurrence(&current, false)
        .map_err(|error| calendar_search_error(schedule, "previous", error))?;
    let next = schedule
        .cron
        .find_next_occurrence(&current, false)
        .map_err(|error| calendar_search_error(schedule, "next", error))?;
    let mut selected = None;
    for candidate in [previous, next] {
        for candidate in physical_occurrences(schedule.timezone, candidate) {
            if candidate <= current {
                continue;
            }
            match selected {
                Some(current) if current <= candidate => {}
                Some(_current) => selected = Some(candidate),
                None => selected = Some(candidate),
            }
        }
    }
    let Some(selected) = selected else {
        return Err(SchedulerError::new(
            SchedulerErrorKind::Calendar,
            format!(
                "cannot select next occurrence for cron {:?}",
                schedule.declaration.cron
            ),
        ));
    };
    Ok(selected)
}

fn physical_occurrences(timezone: Tz, candidate: DateTime<Tz>) -> Vec<DateTime<Tz>> {
    match timezone.from_local_datetime(&candidate.naive_local()) {
        LocalResult::Single(candidate) => vec![candidate],
        LocalResult::Ambiguous(earlier, later) => vec![earlier, later],
        LocalResult::None => vec![candidate],
    }
}

fn calendar_search_error(
    schedule: &CompiledSchedule,
    direction: &str,
    error: croner::errors::CronError,
) -> SchedulerError {
    SchedulerError::new(
        SchedulerErrorKind::Calendar,
        format!(
            "cannot find {direction} occurrence for cron {:?}: {error}",
            schedule.declaration.cron
        ),
    )
}

fn format_schedule_time(time: DateTime<Tz>) -> String {
    time.to_rfc3339_opts(SecondsFormat::Secs, false)
}

fn next_scheduler_id(ids: &mut dyn IdSource) -> Result<String, SchedulerError> {
    ids.next_id()
        .map_err(|error| SchedulerError::new(SchedulerErrorKind::Identity, error.to_string()))
}

fn scheduler_persistence_error(error: DispatchError) -> SchedulerError {
    SchedulerError::new(SchedulerErrorKind::Persistence, error.to_string())
}

fn scheduler_events(dispatch: &Dispatch) -> Result<Vec<Event>, SchedulerError> {
    dispatch
        .list_events(&EventFilter {
            event_type_prefix: Some(SCHEDULER_TICK_PREFIX.to_owned()),
            ..EventFilter::default()
        })
        .map_err(scheduler_persistence_error)
}

fn activate_schedule(
    dispatch: &mut Dispatch,
    schedule: &CompiledSchedule,
    now: DateTime<Utc>,
    now_ms: i64,
    ids: &mut dyn IdSource,
) -> Result<DateTime<Tz>, SchedulerError> {
    let current = now.with_timezone(&schedule.timezone);
    let mut payload = scheduler_payload(schedule);
    payload.insert(
        "catchup".to_owned(),
        optional_string_value(schedule.declaration.catchup.as_deref()),
    );
    payload.insert(
        "observed_at".to_owned(),
        Value::String(format_schedule_time(current)),
    );
    payload.insert("status".to_owned(), Value::String("activated".to_owned()));
    payload.insert(
        "reason".to_owned(),
        Value::String("schedule first observed".to_owned()),
    );
    let id = next_scheduler_id(ids)?;
    let event = Event::from_draft(
        &id,
        now_ms,
        DraftEvent {
            event_type: format!("{SCHEDULER_TICK_PREFIX}activated"),
            source: SCHEDULER_SOURCE.to_owned(),
            payload,
            idempotency_key: Some(activation_key(schedule)),
            caused_by: None,
            session_id: None,
            run_id: None,
            turn_id: None,
        },
    );
    let outcome = dispatch
        .append_trusted_event(event)
        .map_err(scheduler_persistence_error)?;
    let observed_at = required_scheduler_string(&outcome.event, "observed_at")?;
    let observed_at = DateTime::parse_from_rfc3339(observed_at).map_err(|error| {
        SchedulerError::new(
            SchedulerErrorKind::Persistence,
            format!(
                "scheduler event {:?} has invalid observed_at: {error}",
                outcome.event.id
            ),
        )
    })?;
    Ok(observed_at.with_timezone(&schedule.timezone))
}

fn record_missed_schedule(
    dispatch: &mut Dispatch,
    schedule: &CompiledSchedule,
    now: DateTime<Utc>,
    now_ms: i64,
    previous: DateTime<Tz>,
    ids: &mut dyn IdSource,
) -> Result<(), SchedulerError> {
    let current = now.with_timezone(&schedule.timezone);
    if previous.date_naive() == current.date_naive() {
        return Ok(());
    }
    let decisions = scheduler_events(dispatch)?;
    if !schedule_has_prior_activity(&decisions, schedule)
        || schedule_tick_recorded(&decisions, schedule, previous)
    {
        return Ok(());
    }
    let next = next_schedule_time(schedule, now)?;
    let observed_at = now.to_rfc3339_opts(SecondsFormat::Secs, false);
    let id = next_scheduler_id(ids)?;
    let event = Event::from_draft(
        &id,
        now_ms,
        decision_draft(
            schedule,
            previous,
            &observed_at,
            next,
            "missed",
            "previous-day tick not backfilled",
            None,
        ),
    );
    dispatch
        .append_trusted_event(event)
        .map_err(scheduler_persistence_error)?;
    Ok(())
}

fn occurrence_draft(schedule: &CompiledSchedule, scheduled: DateTime<Tz>) -> DraftEvent {
    let scheduled_at = format_schedule_time(scheduled);
    let mut payload = Map::new();
    payload.insert(
        "date".to_owned(),
        Value::String(scheduled.date_naive().to_string()),
    );
    payload.insert("timestamp".to_owned(), Value::String(scheduled_at.clone()));
    DraftEvent {
        event_type: schedule.event_type.clone(),
        source: SCHEDULER_SOURCE.to_owned(),
        payload,
        idempotency_key: Some(format!(
            "schedule:{}:{}:{scheduled_at}",
            schedule.agent, schedule.declaration.cron
        )),
        caused_by: None,
        session_id: None,
        run_id: None,
        turn_id: None,
    }
}

fn decision_draft(
    schedule: &CompiledSchedule,
    scheduled: DateTime<Tz>,
    observed_at: &str,
    next: DateTime<Tz>,
    status: &str,
    reason: &str,
    published_event_id: Option<&str>,
) -> DraftEvent {
    let scheduled_at = format_schedule_time(scheduled);
    let mut payload = scheduler_payload(schedule);
    payload.insert(
        "scheduled_at".to_owned(),
        Value::String(scheduled_at.clone()),
    );
    payload.insert(
        "observed_at".to_owned(),
        Value::String(observed_at.to_owned()),
    );
    payload.insert(
        "next_at".to_owned(),
        Value::String(format_schedule_time(next)),
    );
    payload.insert("status".to_owned(), Value::String(status.to_owned()));
    payload.insert("reason".to_owned(), Value::String(reason.to_owned()));
    payload.insert(
        "published_event_id".to_owned(),
        optional_string_value(published_event_id),
    );
    let timezone = schedule.declaration.timezone.as_deref().unwrap_or("");
    DraftEvent {
        event_type: format!("{SCHEDULER_TICK_PREFIX}{status}"),
        source: SCHEDULER_SOURCE.to_owned(),
        payload,
        idempotency_key: Some(format!(
            "scheduler:{status}:{}:{}:{}:{timezone}:{scheduled_at}",
            schedule.agent, schedule.index, schedule.declaration.cron
        )),
        caused_by: published_event_id.map(str::to_owned),
        session_id: None,
        run_id: None,
        turn_id: None,
    }
}

fn scheduler_payload(schedule: &CompiledSchedule) -> Map<String, Value> {
    let mut payload = Map::new();
    payload.insert("agent".to_owned(), Value::String(schedule.agent.clone()));
    payload.insert("schedule_index".to_owned(), Value::from(schedule.index));
    payload.insert(
        "event_type".to_owned(),
        Value::String(schedule.event_type.clone()),
    );
    payload.insert(
        "cron".to_owned(),
        Value::String(schedule.declaration.cron.clone()),
    );
    payload.insert(
        "timezone".to_owned(),
        optional_string_value(schedule.declaration.timezone.as_deref()),
    );
    payload
}

fn optional_string_value(value: Option<&str>) -> Value {
    match value {
        Some(value) => Value::String(value.to_owned()),
        None => Value::Null,
    }
}

fn activation_key(schedule: &CompiledSchedule) -> String {
    let timezone = schedule.declaration.timezone.as_deref().unwrap_or("");
    let catchup = schedule.declaration.catchup.as_deref().unwrap_or("");
    format!(
        "scheduler:activated:{}:{}:{}:{timezone}:{catchup}",
        schedule.agent, schedule.index, schedule.declaration.cron
    )
}

fn schedule_tick_reason(scheduled: DateTime<Tz>, observed: DateTime<Tz>) -> &'static str {
    if scheduled.date_naive() != observed.date_naive() {
        return "latest catch-up";
    }
    if scheduled.timestamp() / 60 == observed.timestamp() / 60 {
        return "due now";
    }
    "same-day backfill"
}

fn schedule_decision_matches(event: &Event, schedule: &CompiledSchedule) -> bool {
    event.payload.get("agent").and_then(Value::as_str) == Some(schedule.agent.as_str())
        && event.payload.get("schedule_index").and_then(Value::as_u64) == Some(schedule.index)
        && event.payload.get("cron").and_then(Value::as_str)
            == Some(schedule.declaration.cron.as_str())
        && event.payload.get("timezone")
            == Some(&optional_string_value(
                schedule.declaration.timezone.as_deref(),
            ))
}

fn schedule_has_prior_activity(events: &[Event], schedule: &CompiledSchedule) -> bool {
    for event in events {
        if schedule_decision_matches(event, schedule) {
            return true;
        }
    }
    false
}

fn schedule_tick_recorded(
    events: &[Event],
    schedule: &CompiledSchedule,
    scheduled: DateTime<Tz>,
) -> bool {
    let scheduled_at = format_schedule_time(scheduled);
    for event in events {
        if schedule_decision_matches(event, schedule)
            && event.payload.get("scheduled_at").and_then(Value::as_str)
                == Some(scheduled_at.as_str())
        {
            return true;
        }
    }
    false
}

fn required_scheduler_string<'a>(event: &'a Event, field: &str) -> Result<&'a str, SchedulerError> {
    let Some(value) = event.payload.get(field).and_then(Value::as_str) else {
        return Err(SchedulerError::new(
            SchedulerErrorKind::Persistence,
            format!(
                "scheduler event {:?} requires string field {field:?}",
                event.id
            ),
        ));
    };
    Ok(value)
}

/// Classifies a failure while projecting verified authoring data for execution.
///
/// # Examples
///
/// ```
/// let kind = zeta::PrepareAgentErrorKind::DuplicateToolName;
/// assert_eq!(kind.reason(), "duplicate_tool_name");
/// ```
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum PrepareAgentErrorKind {
    /// A project or execution manifest fails verification or projection.
    InvalidManifest,
    /// An authored capability cannot become a native capability declaration.
    InvalidCapability,
    /// Two capabilities resolve to the same model-facing name.
    DuplicateToolName,
    /// A home-relative authored directory has no explicit home directory.
    MissingHomeDirectory,
    /// A resolved execution directory cannot be represented as UTF-8.
    NonUtf8Directory,
}

impl PrepareAgentErrorKind {
    /// Returns a stable machine-readable reason.
    ///
    /// # Examples
    ///
    /// ```
    /// assert_eq!(
    ///     zeta::PrepareAgentErrorKind::MissingHomeDirectory.reason(),
    ///     "missing_home_directory",
    /// );
    /// ```
    pub fn reason(self) -> &'static str {
        match self {
            PrepareAgentErrorKind::InvalidManifest => "invalid_manifest",
            PrepareAgentErrorKind::InvalidCapability => "invalid_capability",
            PrepareAgentErrorKind::DuplicateToolName => "duplicate_tool_name",
            PrepareAgentErrorKind::MissingHomeDirectory => "missing_home_directory",
            PrepareAgentErrorKind::NonUtf8Directory => "non_utf8_directory",
        }
    }
}

/// Reports a pure authored-agent preparation failure.
///
/// # Examples
///
/// ```
/// # fn inspect(error: &zeta::PrepareAgentError) {
/// assert!(!error.detail().is_empty());
/// let _kind = error.kind();
/// # }
/// ```
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PrepareAgentError {
    kind: PrepareAgentErrorKind,
    detail: String,
}

impl PrepareAgentError {
    fn new(kind: PrepareAgentErrorKind, detail: impl Into<String>) -> Self {
        PrepareAgentError {
            kind,
            detail: detail.into(),
        }
    }

    /// Returns the stable failure class.
    ///
    /// # Examples
    ///
    /// ```
    /// # fn inspect(error: &zeta::PrepareAgentError) {
    /// let _kind: zeta::PrepareAgentErrorKind = error.kind();
    /// # }
    /// ```
    pub fn kind(&self) -> PrepareAgentErrorKind {
        self.kind
    }

    /// Returns the human-readable failure detail.
    ///
    /// # Examples
    ///
    /// ```
    /// # fn inspect(error: &zeta::PrepareAgentError) {
    /// let _detail: &str = error.detail();
    /// # }
    /// ```
    pub fn detail(&self) -> &str {
        &self.detail
    }
}

impl fmt::Display for PrepareAgentError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "{}: {}", self.kind.reason(), self.detail)
    }
}

impl std::error::Error for PrepareAgentError {}

/// Selects the executor provider and immutable authored configuration.
///
/// # Examples
///
/// ```
/// # fn inspect(selection: &zeta::ExecutorSelection) {
/// assert!(!selection.provider_id.is_empty());
/// let _config: &serde_json::Map<String, serde_json::Value> = &selection.config;
/// # }
/// ```
#[derive(Clone, Debug, PartialEq)]
pub struct ExecutorSelection {
    /// Names the selected executor provider.
    pub provider_id: String,
    /// Identifies the selected provider implementation.
    pub implementation: ImplementationFingerprint,
    /// Carries the agent's provider-specific configuration.
    pub config: Map<String, Value>,
}

/// Supplies values that vary for each invocation of one prepared agent.
///
/// # Examples
///
/// ```
/// # fn set_objective(mut inputs: zeta::InvocationInputs) {
/// inputs.objective = "Handle this event.".to_owned();
/// # }
/// ```
#[derive(Clone, Debug, PartialEq)]
pub struct InvocationInputs {
    /// States the objective already rendered for this triggering event.
    pub objective: String,
    /// Carries the prior normalized event timeline.
    pub timeline: Vec<Map<String, Value>>,
    /// Adds caller-owned project context.
    pub context: String,
    /// Supplies the directory containing the authored project.
    pub project_directory: PathBuf,
    /// Supplies the home directory used for exact `~` and `~/` expansion.
    pub home_directory: Option<PathBuf>,
    /// Overrides the authored execution directory for this invocation.
    pub base_directory_override: Option<PathBuf>,
    /// Supplies the ISO calendar date shown to the model.
    pub calendar_date: String,
    /// Associates provider transport state with a model session.
    pub model_session_id: Option<String>,
    /// Bounds model calls while allowing a final pending tool batch.
    pub max_model_calls: usize,
    /// Supplies the model-facing output-token budget.
    pub max_tokens: u64,
    /// Selects the model's tool behavior.
    pub tool_choice: Value,
    /// Stabilizes side-effect identities across retries.
    pub effect_scope: Option<String>,
    /// Enables retry-stable control handles for a queue item.
    pub source_queue_item_id: Option<String>,
    /// Associates cancellation requests with the triggering session.
    pub source_session_id: Option<String>,
    /// Names the causal parent of the first model proposal.
    pub caused_by: Option<String>,
    /// Names the producer on model, tool, and turn drafts.
    pub event_source: String,
    /// Associates every emitted draft with one session.
    pub session_id: Option<String>,
    /// Associates every emitted draft with one run.
    pub run_id: Option<String>,
    /// Associates every emitted draft with one turn.
    pub turn_id: Option<String>,
    /// Selects deterministic prompt compaction behavior.
    pub prompt_transform: PromptTransform,
    /// Reports the caller's context threshold to budget queries.
    pub compaction_threshold_tokens: Option<usize>,
    /// Stops the run at this caller-defined clock value.
    pub deadline_ms: Option<i64>,
}

/// Holds one verified, immutable authored projection for repeated invocations.
///
/// # Examples
///
/// ```
/// # fn inspect(agent: &zeta::PreparedAgent) {
/// assert!(!agent.agent_slug().is_empty());
/// let _capabilities: &[zeta_agent::ResolvedCapability] = agent.capabilities();
/// # }
/// ```
#[derive(Clone, Debug, PartialEq)]
pub struct PreparedAgent {
    execution_manifest_id: ExecutionManifestId,
    project_revision_id: ProjectRevisionId,
    agent_slug: String,
    agent_description: String,
    capabilities: Vec<ResolvedCapability>,
    model_name: Option<String>,
    model_url: Option<String>,
    model_api: String,
    thinking: Option<String>,
    tool_profile: ToolProfile,
    publishable_events: Map<String, Value>,
    executor_selection: ExecutorSelection,
    authored_base_directory: Option<PathBuf>,
}

impl PreparedAgent {
    /// Returns the verified execution-manifest identity.
    ///
    /// # Examples
    ///
    /// ```
    /// # fn inspect(agent: &zeta::PreparedAgent) {
    /// let _id: zeta_manifest::ExecutionManifestId = agent.execution_manifest_id();
    /// # }
    /// ```
    pub fn execution_manifest_id(&self) -> ExecutionManifestId {
        self.execution_manifest_id
    }

    /// Returns the verified project-revision identity.
    ///
    /// # Examples
    ///
    /// ```
    /// # fn inspect(agent: &zeta::PreparedAgent) {
    /// let _id: zeta_manifest::ProjectRevisionId = agent.project_revision_id();
    /// # }
    /// ```
    pub fn project_revision_id(&self) -> ProjectRevisionId {
        self.project_revision_id
    }

    /// Returns the authored agent slug.
    ///
    /// # Examples
    ///
    /// ```
    /// # fn inspect(agent: &zeta::PreparedAgent) {
    /// let _slug: &str = agent.agent_slug();
    /// # }
    /// ```
    pub fn agent_slug(&self) -> &str {
        &self.agent_slug
    }

    /// Returns the authored agent description used as the base system prompt.
    ///
    /// # Examples
    ///
    /// ```
    /// # fn inspect(agent: &zeta::PreparedAgent) {
    /// let _description: &str = agent.agent_description();
    /// # }
    /// ```
    pub fn agent_description(&self) -> &str {
        &self.agent_description
    }

    /// Returns resolved capabilities in authored grant order.
    ///
    /// # Examples
    ///
    /// ```
    /// # fn inspect(agent: &zeta::PreparedAgent) {
    /// let _capabilities: &[zeta_agent::ResolvedCapability] = agent.capabilities();
    /// # }
    /// ```
    pub fn capabilities(&self) -> &[ResolvedCapability] {
        &self.capabilities
    }

    /// Returns the selected executor provider and authored configuration.
    ///
    /// # Examples
    ///
    /// ```
    /// # fn inspect(agent: &zeta::PreparedAgent) {
    /// let _selection: &zeta::ExecutorSelection = agent.executor_selection();
    /// # }
    /// ```
    pub fn executor_selection(&self) -> &ExecutorSelection {
        &self.executor_selection
    }

    /// Constructs one portable agent invocation from explicit per-run inputs.
    ///
    /// # Errors
    ///
    /// Returns [`PrepareAgentError`] when a home-relative directory has no
    /// supplied home or the resolved directory is not valid UTF-8.
    ///
    /// # Examples
    ///
    /// ```
    /// # fn invoke(
    /// #     agent: &zeta::PreparedAgent,
    /// #     inputs: zeta::InvocationInputs,
    /// # ) -> Result<(), zeta::PrepareAgentError> {
    /// let invocation = agent.invocation(inputs)?;
    /// assert_eq!(invocation.source_agent_id.as_deref(), Some(agent.agent_slug()));
    /// # Ok(())
    /// # }
    /// ```
    pub fn invocation(
        &self,
        inputs: InvocationInputs,
    ) -> Result<AgentInvocation, PrepareAgentError> {
        let InvocationInputs {
            objective,
            timeline,
            context,
            project_directory,
            home_directory,
            base_directory_override,
            calendar_date,
            model_session_id,
            max_model_calls,
            max_tokens,
            tool_choice,
            effect_scope,
            source_queue_item_id,
            source_session_id,
            caused_by,
            event_source,
            session_id,
            run_id,
            turn_id,
            prompt_transform,
            compaction_threshold_tokens,
            deadline_ms,
        } = inputs;
        let base_directory = resolve_base_directory(
            &project_directory,
            home_directory.as_deref(),
            base_directory_override,
            self.authored_base_directory.as_deref(),
        )?;
        let mut allowed_capabilities = Vec::with_capacity(self.capabilities.len());
        for capability in &self.capabilities {
            allowed_capabilities.push(capability.canonical.id.clone());
        }
        Ok(AgentInvocation {
            objective,
            timeline,
            context,
            system_prompt: Some(self.agent_description.clone()),
            allowed_capabilities,
            tool_profile: self.tool_profile,
            max_model_calls,
            model_name: self.model_name.clone(),
            model_url: self.model_url.clone(),
            model_api: Some(self.model_api.clone()),
            thinking: self.thinking.clone(),
            model_session_id,
            max_tokens,
            tool_choice,
            base_directory: Some(base_directory.clone()),
            effect_scope,
            source_queue_item_id,
            source_agent_id: Some(self.agent_slug.clone()),
            source_session_id,
            caused_by,
            event_source,
            session_id,
            run_id,
            turn_id,
            environment: PromptEnvironment {
                working_directory: base_directory,
                calendar_date,
            },
            prompt_transform,
            compaction_threshold_tokens,
            deadline_ms,
            publishable_events: self.publishable_events.clone(),
        })
    }
}

/// Verifies and projects one authored execution manifest for repeated runs.
///
/// # Errors
///
/// Returns [`PrepareAgentError`] when the manifests fail verification, a
/// capability cannot be projected, or model-facing tool names collide.
///
/// # Examples
///
/// ```
/// # fn prepare(
/// #     project: &zeta_manifest::ProjectManifest,
/// #     execution: &zeta_manifest::ExecutionManifest,
/// # ) -> Result<(), zeta::PrepareAgentError> {
/// let agent = zeta::prepare_agent(project, execution)?;
/// assert_eq!(agent.execution_manifest_id(), execution.id);
/// # Ok(())
/// # }
/// ```
pub fn prepare_agent(
    project: &ProjectManifest,
    execution: &ExecutionManifest,
) -> Result<PreparedAgent, PrepareAgentError> {
    verify_execution_manifest(execution, project).map_err(|error| {
        PrepareAgentError::new(PrepareAgentErrorKind::InvalidManifest, error.to_string())
    })?;
    let (model_name, model_url) = resolved_model_endpoint(execution);
    let (model_api, thinking, tool_profile) = resolved_model_contract(execution)?;
    let capabilities = resolved_capabilities(execution, tool_profile)?;
    let publishable_events = publishable_events(execution)?;
    Ok(PreparedAgent {
        execution_manifest_id: execution.id,
        project_revision_id: execution.project_revision,
        agent_slug: execution.agent.slug.clone(),
        agent_description: execution.agent.description.clone(),
        capabilities,
        model_name,
        model_url,
        model_api,
        thinking,
        tool_profile,
        publishable_events,
        executor_selection: ExecutorSelection {
            provider_id: execution.executor_provider.id.clone(),
            implementation: execution.executor_provider.implementation.clone(),
            config: execution.agent.executor.config.clone(),
        },
        authored_base_directory: execution.agent.base_dir.clone(),
    })
}

fn resolved_model_endpoint(execution: &ExecutionManifest) -> (Option<String>, Option<String>) {
    let project = &execution.model;
    let model_name = project.as_ref().map(|project| project.model.clone());
    let model_url = project.as_ref().map(|project| project.url.clone());
    (model_name, model_url)
}

fn resolved_model_contract(
    execution: &ExecutionManifest,
) -> Result<(String, Option<String>, ToolProfile), PrepareAgentError> {
    let Some(model) = &execution.model else {
        return Ok(("chat-completions".to_owned(), None, ToolProfile::Native));
    };
    let profile = match model.tool_profile.as_str() {
        "native" => ToolProfile::Native,
        "codex" => ToolProfile::Codex,
        value => {
            return Err(PrepareAgentError::new(
                PrepareAgentErrorKind::InvalidManifest,
                format!("unsupported verified tool profile {value:?}"),
            ));
        }
    };
    Ok((model.api.clone(), model.thinking.clone(), profile))
}

fn resolved_capabilities(
    execution: &ExecutionManifest,
    profile: ToolProfile,
) -> Result<Vec<ResolvedCapability>, PrepareAgentError> {
    let mut resolved = Vec::with_capacity(execution.agent.tools.len());
    let mut names = BTreeSet::new();
    for id in &execution.agent.tools {
        let Some(authored) = execution.capabilities.get(id) else {
            return Err(PrepareAgentError::new(
                PrepareAgentErrorKind::InvalidManifest,
                format!("verified execution manifest omits capability {id:?}"),
            ));
        };
        let id = authored.id.as_str().parse().map_err(|error: AgentError| {
            PrepareAgentError::new(PrepareAgentErrorKind::InvalidCapability, error.to_string())
        })?;
        let capability = Capability {
            id,
            description: authored.description.clone(),
            input_schema: authored.input_schema.clone(),
            delivery_semantics: authored.delivery_semantics.map(delivery_semantics),
        };
        let capability = profile.resolve_capability(&capability, &authored.name);
        if !names.insert(capability.model_name.clone()) {
            return Err(PrepareAgentError::new(
                PrepareAgentErrorKind::DuplicateToolName,
                format!(
                    "multiple capabilities resolve to model-facing name {:?}",
                    capability.model_name
                ),
            ));
        }
        resolved.push(capability);
    }
    Ok(resolved)
}

fn delivery_semantics(value: AuthoredDeliverySemantics) -> AgentDeliverySemantics {
    match value {
        AuthoredDeliverySemantics::IdempotentWithKey => AgentDeliverySemantics::IdempotentWithKey,
        AuthoredDeliverySemantics::ConnectorDeduplicated => {
            AgentDeliverySemantics::ConnectorDeduplicated
        }
        AuthoredDeliverySemantics::AtLeastOnce => AgentDeliverySemantics::AtLeastOnce,
        AuthoredDeliverySemantics::UnsafeToRetry => AgentDeliverySemantics::UnsafeToRetry,
    }
}

fn publishable_events(
    execution: &ExecutionManifest,
) -> Result<Map<String, Value>, PrepareAgentError> {
    let mut publishable = Map::new();
    for event_type in &execution.agent.publishes {
        let Some(schema) = execution.events.schema(event_type) else {
            return Err(PrepareAgentError::new(
                PrepareAgentErrorKind::InvalidManifest,
                format!("verified execution manifest omits event {event_type:?}"),
            ));
        };
        let schema = match schema {
            Some(schema) => Value::Object(schema.clone()),
            None => Value::Null,
        };
        publishable.insert(event_type.clone(), schema);
    }
    Ok(publishable)
}

fn resolve_base_directory(
    project_directory: &Path,
    home_directory: Option<&Path>,
    override_directory: Option<PathBuf>,
    authored_directory: Option<&Path>,
) -> Result<String, PrepareAgentError> {
    let directory = if let Some(directory) = override_directory {
        directory
    } else if let Some(directory) = authored_directory {
        resolve_authored_directory(project_directory, home_directory, directory)?
    } else {
        project_directory.to_path_buf()
    };
    let Some(directory) = directory.to_str() else {
        return Err(PrepareAgentError::new(
            PrepareAgentErrorKind::NonUtf8Directory,
            "resolved base directory must be valid UTF-8",
        ));
    };
    Ok(directory.to_owned())
}

fn resolve_authored_directory(
    project_directory: &Path,
    home_directory: Option<&Path>,
    authored_directory: &Path,
) -> Result<PathBuf, PrepareAgentError> {
    if authored_directory.is_absolute() {
        return Ok(authored_directory.to_path_buf());
    }
    let Some(directory) = authored_directory.to_str() else {
        return Err(PrepareAgentError::new(
            PrepareAgentErrorKind::NonUtf8Directory,
            "authored base directory must be valid UTF-8",
        ));
    };
    if directory == "~" || directory.starts_with("~/") {
        let Some(home_directory) = home_directory else {
            return Err(PrepareAgentError::new(
                PrepareAgentErrorKind::MissingHomeDirectory,
                "home-relative base directory requires an explicit home directory",
            ));
        };
        if directory == "~" {
            return Ok(home_directory.to_path_buf());
        }
        return Ok(home_directory.join(&directory[2..]));
    }
    Ok(project_directory.join(authored_directory))
}

/// Reads Unix time from the operating-system clock.
#[derive(Clone, Copy, Debug, Default)]
pub struct SystemClock;

impl Clock for SystemClock {
    fn now_millis(&self) -> i64 {
        let elapsed = SystemTime::now().duration_since(UNIX_EPOCH);
        let Ok(elapsed) = elapsed else {
            return 0;
        };
        i64::try_from(elapsed.as_millis()).unwrap_or(i64::MAX)
    }
}

/// Shares the first cooperative abort reason across threads.
#[derive(Clone, Debug, Default)]
pub struct CancellationToken {
    reason: Arc<Mutex<Option<AbortReason>>>,
}

impl CancellationToken {
    /// Creates an active cancellation token.
    ///
    /// # Examples
    ///
    /// ```
    /// use zeta_agent::{AbortReason, AbortSignal};
    ///
    /// let token = zeta::CancellationToken::new();
    /// assert_eq!(token.reason(), None);
    /// assert!(token.cancel(AbortReason::Cancelled));
    /// assert_eq!(token.reason(), Some(AbortReason::Cancelled));
    /// ```
    pub fn new() -> Self {
        Self::default()
    }

    /// Records the first abort reason and reports whether this call won.
    pub fn cancel(&self, reason: AbortReason) -> bool {
        let Ok(mut current) = self.reason.lock() else {
            return false;
        };
        if current.is_some() {
            return false;
        }
        *current = Some(reason);
        true
    }
}

impl AbortSignal for CancellationToken {
    fn reason(&self) -> Option<AbortReason> {
        self.reason.lock().ok().and_then(|reason| *reason)
    }
}

/// Generates opaque UUID version 4 identities with one stable prefix.
#[derive(Clone, Debug)]
pub struct UuidIdSource {
    prefix: String,
}

impl UuidIdSource {
    /// Creates an identity source with a non-empty namespace prefix.
    ///
    /// # Examples
    ///
    /// ```
    /// use zeta_agent::IdSource;
    ///
    /// let mut source = zeta::UuidIdSource::new("event");
    /// assert!(source.next_id().unwrap().starts_with("event_"));
    /// ```
    pub fn new(prefix: impl Into<String>) -> Self {
        Self {
            prefix: prefix.into(),
        }
    }
}

impl IdSource for UuidIdSource {
    fn next_id(&mut self) -> Result<String, AgentError> {
        let prefix = self.prefix.trim();
        if prefix.is_empty() {
            return Err(AgentError::identity(
                "UUID identity prefix must not be empty",
            ));
        }
        Ok(format!("{prefix}_{}", Uuid::new_v4()))
    }
}

/// Forwards transient observations to an application callback.
pub struct CallbackObserver<F> {
    callback: F,
}

impl<F> CallbackObserver<F> {
    /// Creates an observer backed by one callback.
    pub fn new(callback: F) -> Self {
        Self { callback }
    }
}

impl<F> AgentObserver for CallbackObserver<F>
where
    F: FnMut(Observation),
{
    fn observe(&mut self, observation: Observation) {
        (self.callback)(observation);
    }
}

/// Forwards complete durable drafts to an application callback.
pub struct CallbackDraftRecorder<F> {
    callback: F,
}

impl<F> CallbackDraftRecorder<F> {
    /// Creates a draft recorder backed by one callback.
    pub fn new(callback: F) -> Self {
        Self { callback }
    }
}

impl<F> DraftRecorder for CallbackDraftRecorder<F>
where
    F: FnMut(&str, &DraftEvent) -> Result<String, String>,
{
    fn record(&mut self, event_id: &str, draft: &DraftEvent) -> Result<String, AgentError> {
        (self.callback)(event_id, draft).map_err(|error| AgentError::durability(error.to_string()))
    }
}
