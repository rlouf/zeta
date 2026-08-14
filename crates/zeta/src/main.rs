use std::env;
use std::ffi::{OsStr, OsString};
use std::fmt;
use std::fs::{self, File};
use std::io::{self, BufRead, BufReader as StdBufReader, Write};
use std::os::unix::fs::PermissionsExt;
use std::path::{Component, Path, PathBuf};
use std::process::{Command as ProcessCommand, ExitCode, Stdio};
use std::sync::{mpsc, Arc, Mutex};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

#[cfg(test)]
use clap::CommandFactory;
use clap::{Args, Parser, Subcommand};
use rustix::fs::{flock, open, FlockOperation, Mode, OFlags};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::net::unix::{OwnedReadHalf, OwnedWriteHalf};
use tokio::net::UnixStream;
use zeta::runtime_services::{
    RuntimeLease, RuntimeLifecycleErrorKind, RuntimeMetadata, RuntimeOwnerMode, RuntimePaths,
    RuntimePhase,
};
use zeta::{
    ActiveProjectStatus, LocalSocketConfig, LocalSocketServer, Project, ProjectRevision, Runtime,
    RuntimeStatus,
};
use zeta_ipc::{
    Action, ErrorObject, InitializeParams, Message, PeerIdentity, Request, RequestId, Retryability,
    Role, Session, ShutdownDirection, INVALID_PARAMS, MAX_FRAME_BYTES, METHOD_NOT_FOUND,
    PROTOCOL_VERSION, SERVER_ERROR,
};
use zeta_journal::DraftEvent;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum UpMode {
    Foreground,
    Detached,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum StatusOutput {
    Human,
    Json,
}

#[derive(Debug, Parser)]
#[command(
    name = "zeta",
    about = "Run one explicit Zeta agent project.",
    long_about = "Run one explicit Zeta agent project.\n\nReload validates source and activates one immutable project revision. The runtime uses that revision until the next successful reload.",
    version,
    arg_required_else_help = true,
    disable_help_subcommand = true,
    after_help = "GET STARTED:\n  1. zeta check --project-root ~/my-project\n  2. zeta reload --project-root ~/my-project\n  3. zeta up --detach --project-root ~/my-project\n  4. zeta status\n\nRun 'zeta <command> --help' for command examples."
)]
struct Cli {
    #[command(subcommand)]
    command: CliCommand,
}

#[derive(Debug, Subcommand)]
enum CliCommand {
    /// Validate project source without runtime changes.
    #[command(
        long_about = "Validate Markdown agent files in agents/. This command does not change the active revision or start the runtime.",
        after_help = "EXAMPLE:\n  zeta check --project-root ~/my-project"
    )]
    Check(ProjectArgs),
    /// Validate source and activate a new project revision.
    #[command(
        long_about = "Validate Markdown agent files in agents/ and atomically activate one project revision. A running runtime adopts it immediately. A failed reload keeps the prior revision active.",
        after_help = "EXAMPLES:\n  zeta reload --project-root ~/my-project\n  zeta reload --project-root ~/my-project --state-dir /var/lib/zeta"
    )]
    Reload(ReloadArgs),
    /// Start the runtime with the active project revision.
    #[command(
        long_about = "Start the runtime with the active project revision. Run 'zeta reload' first. This command does not read draft agent files.",
        after_help = "EXAMPLES:\n  zeta up --project-root ~/my-project\n  zeta up --detach --project-root ~/my-project"
    )]
    Up(UpArgs),
    /// Stop the runtime and preserve its active revision.
    #[command(
        long_about = "Stop the runtime. This command keeps the active project revision. Use 'zeta up' to start that revision again.",
        after_help = "EXAMPLE:\n  zeta down"
    )]
    Down(StateArgs),
    /// Show runtime, revision, and agent status.
    #[command(
        long_about = "Show the runtime state. A running runtime also reports its active project revision and agents. Use --json for script input.",
        after_help = "EXAMPLES:\n  zeta status\n  zeta status --json | jq '.active_project.agents[].slug'"
    )]
    Status(StatusArgs),
}

#[derive(Debug, Args)]
struct UpArgs {
    /// Start in the background and return after readiness.
    #[arg(short = 'd', long)]
    detach: bool,
    /// Find the active project revision from this directory.
    #[arg(long, value_name = "DIR", default_value = ".")]
    project_root: PathBuf,
    /// Read runtime state from this directory.
    #[arg(long, value_name = "DIR")]
    state_dir: Option<PathBuf>,
}

#[derive(Debug, Args)]
struct ProjectArgs {
    /// Read authored project files from this directory.
    #[arg(long, value_name = "DIR", default_value = ".")]
    project_root: PathBuf,
}

#[derive(Debug, Args)]
struct ReloadArgs {
    /// Read authored project files from this directory.
    #[arg(long, value_name = "DIR", default_value = ".")]
    project_root: PathBuf,
    /// Store the active project revision in this directory.
    #[arg(long, value_name = "DIR")]
    state_dir: Option<PathBuf>,
}

#[derive(Debug, Args)]
struct StateArgs {
    /// Find runtime state from this project directory.
    #[arg(long, value_name = "DIR")]
    project_root: Option<PathBuf>,
    /// Read runtime state from this directory.
    #[arg(long, value_name = "DIR")]
    state_dir: Option<PathBuf>,
}

#[derive(Debug, Args)]
struct StatusArgs {
    /// Find runtime state from this project directory.
    #[arg(long, value_name = "DIR")]
    project_root: Option<PathBuf>,
    /// Read runtime state from this directory.
    #[arg(long, value_name = "DIR")]
    state_dir: Option<PathBuf>,
    /// Write one stable JSON status document.
    #[arg(long)]
    json: bool,
}

#[derive(Debug, Eq, PartialEq)]
enum Command {
    Check {
        project_root: PathBuf,
    },
    Reload {
        project_root: PathBuf,
        state_dir: Option<PathBuf>,
    },
    Up {
        mode: UpMode,
        project_root: PathBuf,
        state_dir: Option<PathBuf>,
    },
    Down {
        project_root: Option<PathBuf>,
        state_dir: Option<PathBuf>,
    },
    Status {
        project_root: Option<PathBuf>,
        state_dir: Option<PathBuf>,
        output: StatusOutput,
    },
}

impl From<CliCommand> for Command {
    fn from(command: CliCommand) -> Self {
        match command {
            CliCommand::Check(arguments) => Self::Check {
                project_root: arguments.project_root,
            },
            CliCommand::Reload(arguments) => Self::Reload {
                project_root: arguments.project_root,
                state_dir: arguments.state_dir,
            },
            CliCommand::Up(arguments) => Self::Up {
                mode: if arguments.detach {
                    UpMode::Detached
                } else {
                    UpMode::Foreground
                },
                project_root: arguments.project_root,
                state_dir: arguments.state_dir,
            },
            CliCommand::Down(arguments) => Self::Down {
                project_root: arguments.project_root,
                state_dir: arguments.state_dir,
            },
            CliCommand::Status(arguments) => Self::Status {
                project_root: arguments.project_root,
                state_dir: arguments.state_dir,
                output: if arguments.json {
                    StatusOutput::Json
                } else {
                    StatusOutput::Human
                },
            },
        }
    }
}

#[derive(Debug, Eq, PartialEq)]
struct PathError(String);

impl fmt::Display for PathError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.0)
    }
}

#[derive(Debug, Eq, PartialEq)]
enum ResolvedCommand {
    Check {
        project_root: PathBuf,
    },
    Reload {
        project_root: PathBuf,
        state_dir: PathBuf,
    },
    Up {
        mode: UpMode,
        project_root: PathBuf,
        state_dir: PathBuf,
    },
    Down {
        state_dir: PathBuf,
    },
    Status {
        state_dir: PathBuf,
        output: StatusOutput,
    },
}

#[cfg(test)]
impl ResolvedCommand {
    fn state_dir(&self) -> &Path {
        match self {
            Self::Check { project_root: _ } => {
                panic!("check does not resolve a runtime state directory")
            }
            Self::Reload {
                project_root: _,
                state_dir,
            } => state_dir,
            Self::Up {
                mode: _,
                project_root: _,
                state_dir,
            } => state_dir,
            Self::Down { state_dir } => state_dir,
            Self::Status {
                state_dir,
                output: _,
            } => state_dir,
        }
    }
}

#[tokio::main(flavor = "current_thread")]
async fn main() -> ExitCode {
    let arguments: Vec<OsString> = env::args_os().collect();
    if arguments.get(1) == Some(&OsString::from("--daemon-child")) {
        return run_daemon_child(&arguments[1..]).await;
    }
    let cli = match Cli::try_parse_from(arguments) {
        Ok(cli) => cli,
        Err(error) => {
            let code = error.exit_code();
            let _printed = error.print();
            return ExitCode::from(u8::try_from(code).unwrap_or(2));
        }
    };
    let command = Command::from(cli.command);
    let invocation_dir = match env::current_dir() {
        Ok(path) => path,
        Err(error) => {
            eprintln!("error: cannot resolve the invocation directory: {error}");
            return ExitCode::from(1);
        }
    };
    let Some(home) = env::var_os("HOME") else {
        eprintln!("error: HOME is not set");
        return ExitCode::from(1);
    };
    let environment_state_dir = env::var_os("ZETA_STATE_DIR");
    let resolved = resolve_command_paths(
        command,
        &invocation_dir,
        environment_state_dir.as_deref(),
        Path::new(&home),
    );
    let resolved = match resolved {
        Ok(resolved) => resolved,
        Err(error) => {
            eprintln!("error: {error}");
            return ExitCode::from(1);
        }
    };
    match execute_command(resolved).await {
        Ok(code) => code,
        Err(error) => {
            print_command_error(&error);
            ExitCode::from(1)
        }
    }
}

fn print_command_error(error: &str) {
    eprintln!("error: {error}");
    let hint = if error.contains("no active project revision") {
        Some("Run 'zeta reload --project-root <PROJECT_ROOT>' first.")
    } else if error.contains("different project root") {
        Some("Use the same --project-root and --state-dir for each command.")
    } else if error.contains("no enabled agents") {
        Some("Set enabled: true in at least one Markdown agent file.")
    } else if error.contains("agents directory") {
        Some("Create <PROJECT_ROOT>/agents/ and add one Markdown agent file.")
    } else if error.contains("runtime is degraded") {
        Some("Run 'zeta status --json' to inspect the runtime state.")
    } else {
        None
    };
    if let Some(hint) = hint {
        eprintln!("help: {hint}");
    }
}

fn resolve_command_paths(
    command: Command,
    invocation_dir: &Path,
    environment_state_dir: Option<&OsStr>,
    home: &Path,
) -> Result<ResolvedCommand, PathError> {
    let invocation_dir = canonical_directory(invocation_dir, "invocation directory")?;
    let home = resolve_allow_missing(home, &invocation_dir)?;
    match command {
        Command::Check { project_root } => {
            let project_root = resolve_project_root(project_root, &home, &invocation_dir)?;
            Ok(ResolvedCommand::Check { project_root })
        }
        Command::Reload {
            project_root,
            state_dir,
        } => {
            let project_root = resolve_project_root(project_root, &home, &invocation_dir)?;
            let state_dir = resolve_state_dir(
                state_dir.as_deref(),
                environment_state_dir,
                &project_root,
                &invocation_dir,
                &home,
            )?;
            Ok(ResolvedCommand::Reload {
                project_root,
                state_dir,
            })
        }
        Command::Up {
            mode,
            project_root,
            state_dir,
        } => {
            let project_root = resolve_project_root(project_root, &home, &invocation_dir)?;
            let state_dir = resolve_state_dir(
                state_dir.as_deref(),
                environment_state_dir,
                &project_root,
                &invocation_dir,
                &home,
            )?;
            Ok(ResolvedCommand::Up {
                mode,
                project_root,
                state_dir,
            })
        }
        Command::Down {
            project_root,
            state_dir,
        } => {
            let state_start = resolve_state_start(project_root, &home, &invocation_dir)?;
            let state_dir = resolve_state_dir(
                state_dir.as_deref(),
                environment_state_dir,
                &state_start,
                &invocation_dir,
                &home,
            )?;
            Ok(ResolvedCommand::Down { state_dir })
        }
        Command::Status {
            project_root,
            state_dir,
            output,
        } => {
            let state_start = resolve_state_start(project_root, &home, &invocation_dir)?;
            let state_dir = resolve_state_dir(
                state_dir.as_deref(),
                environment_state_dir,
                &state_start,
                &invocation_dir,
                &home,
            )?;
            Ok(ResolvedCommand::Status { state_dir, output })
        }
    }
}

fn resolve_project_root(
    project_root: PathBuf,
    home: &Path,
    invocation_dir: &Path,
) -> Result<PathBuf, PathError> {
    let project_root = expand_user(&project_root, home);
    let project_root = resolve_allow_missing(&project_root, invocation_dir)?;
    canonical_directory(&project_root, "project root")
}

fn resolve_state_start(
    project_root: Option<PathBuf>,
    home: &Path,
    invocation_dir: &Path,
) -> Result<PathBuf, PathError> {
    match project_root {
        Some(project_root) => resolve_project_root(project_root, home, invocation_dir),
        None => Ok(invocation_dir.to_path_buf()),
    }
}

fn resolve_state_dir(
    explicit: Option<&Path>,
    environment: Option<&OsStr>,
    start: &Path,
    invocation_dir: &Path,
    home: &Path,
) -> Result<PathBuf, PathError> {
    if let Some(explicit) = explicit {
        let explicit = expand_user(explicit, home);
        return resolve_selected_state(&explicit, invocation_dir);
    }
    if let Some(environment) = environment {
        if !environment.is_empty() {
            let environment = expand_user(Path::new(environment), home);
            return resolve_selected_state(&environment, invocation_dir);
        }
    }
    discover_state_dir(start, home)
}

fn resolve_selected_state(path: &Path, invocation_dir: &Path) -> Result<PathBuf, PathError> {
    let path = resolve_allow_missing(path, invocation_dir)?;
    match fs::metadata(&path) {
        Ok(metadata) => {
            if !metadata.is_dir() {
                return Err(invalid_state_marker(&path));
            }
            fs::canonicalize(&path).map_err(|error| {
                PathError(format!(
                    "cannot resolve runtime state directory '{}': {error}",
                    path.display()
                ))
            })
        }
        Err(error) => {
            if error.kind() != std::io::ErrorKind::NotFound {
                return Err(PathError(format!(
                    "cannot inspect runtime state directory '{}': {error}",
                    path.display()
                )));
            }
            if fs::symlink_metadata(&path).is_ok() {
                return Err(invalid_state_marker(&path));
            }
            Ok(path)
        }
    }
}

fn discover_state_dir(start: &Path, home: &Path) -> Result<PathBuf, PathError> {
    let start = canonical_directory(start, "state discovery start")?;
    let exclude_home = start != home && start.starts_with(home);
    let mut current = Some(start.as_path());
    while let Some(root) = current {
        if exclude_home && root == home {
            break;
        }
        let marker = root.join(".zeta");
        match fs::metadata(&marker) {
            Ok(metadata) => {
                if metadata.is_dir() {
                    return fs::canonicalize(&marker).map_err(|error| {
                        PathError(format!(
                            "cannot resolve runtime state marker '{}': {error}",
                            marker.display()
                        ))
                    });
                }
                return Err(invalid_state_marker(&marker));
            }
            Err(error) => {
                if error.kind() != std::io::ErrorKind::NotFound {
                    return Err(PathError(format!(
                        "cannot inspect runtime state marker '{}': {error}",
                        marker.display()
                    )));
                }
                if fs::symlink_metadata(&marker).is_ok() {
                    return Err(invalid_state_marker(&marker));
                }
            }
        }
        current = root.parent();
    }
    Ok(start.join(".zeta"))
}

fn canonical_directory(path: &Path, label: &str) -> Result<PathBuf, PathError> {
    let path = fs::canonicalize(path)
        .map_err(|_error| PathError(format!("{label} is not a directory: {}", path.display())))?;
    if !path.is_dir() {
        return Err(PathError(format!(
            "{label} is not a directory: {}",
            path.display()
        )));
    }
    Ok(path)
}

fn resolve_allow_missing(path: &Path, base: &Path) -> Result<PathBuf, PathError> {
    let path = if path.is_absolute() {
        path.to_path_buf()
    } else {
        base.join(path)
    };
    let path = normalize_absolute(&path);
    if path.exists() {
        return fs::canonicalize(&path).map_err(|error| {
            PathError(format!("cannot resolve path '{}': {error}", path.display()))
        });
    }
    let mut ancestor = path.as_path();
    let mut missing = Vec::new();
    while !ancestor.exists() {
        let Some(name) = ancestor.file_name() else {
            return Ok(path);
        };
        missing.push(name.to_os_string());
        let Some(parent) = ancestor.parent() else {
            return Ok(path);
        };
        ancestor = parent;
    }
    let mut resolved = fs::canonicalize(ancestor)
        .map_err(|error| PathError(format!("cannot resolve path '{}': {error}", path.display())))?;
    for name in missing.into_iter().rev() {
        resolved.push(name);
    }
    Ok(resolved)
}

fn normalize_absolute(path: &Path) -> PathBuf {
    let mut normalized = PathBuf::new();
    for component in path.components() {
        match component {
            Component::Prefix(prefix) => normalized.push(prefix.as_os_str()),
            Component::RootDir => normalized.push(component.as_os_str()),
            Component::CurDir => {}
            Component::ParentDir => {
                normalized.pop();
            }
            Component::Normal(part) => normalized.push(part),
        }
    }
    normalized
}

fn expand_user(path: &Path, home: &Path) -> PathBuf {
    if path == Path::new("~") {
        return home.to_path_buf();
    }
    let Ok(suffix) = path.strip_prefix("~") else {
        return path.to_path_buf();
    };
    home.join(suffix)
}

fn invalid_state_marker(path: &Path) -> PathError {
    PathError(format!(
        "runtime state marker is not a directory: {}",
        path.display()
    ))
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
enum DaemonStatus {
    Running,
    Stopping,
    Stopped,
    Degraded,
    Stale,
}

#[derive(Debug, Serialize)]
struct StatusReport {
    schema: &'static str,
    version: u64,
    status: DaemonStatus,
    pid: Option<u32>,
    instance_id: Option<String>,
    project_root: Option<PathBuf>,
    state_dir: PathBuf,
    socket: PathBuf,
    control_socket: PathBuf,
    detail: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    active_project: Option<ActiveProjectStatus>,
    #[serde(skip_serializing_if = "Option::is_none")]
    dispatch: Option<RuntimeStatus>,
}

impl StatusReport {
    fn from_metadata(
        status: DaemonStatus,
        paths: &RuntimePaths,
        metadata: RuntimeMetadata,
        detail: Option<String>,
    ) -> Self {
        Self {
            schema: "zeta.status",
            version: 2,
            status,
            pid: Some(metadata.pid),
            instance_id: Some(metadata.instance_id),
            project_root: Some(metadata.project_root),
            state_dir: paths.state_dir().to_path_buf(),
            socket: paths.socket().to_path_buf(),
            control_socket: paths.control_socket().to_path_buf(),
            detail,
            active_project: None,
            dispatch: None,
        }
    }

    fn without_metadata(
        status: DaemonStatus,
        paths: &RuntimePaths,
        detail: Option<String>,
    ) -> Self {
        Self {
            schema: "zeta.status",
            version: 2,
            status,
            pid: None,
            instance_id: None,
            project_root: None,
            state_dir: paths.state_dir().to_path_buf(),
            socket: paths.socket().to_path_buf(),
            control_socket: paths.control_socket().to_path_buf(),
            detail,
            active_project: None,
            dispatch: None,
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum LeaseProbe {
    Free,
    Held,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
enum DaemonHandshakeStatus {
    Ready,
    AlreadyRunning,
    Error,
}

#[derive(Debug, Deserialize, Serialize)]
struct DaemonHandshake {
    nonce: String,
    status: DaemonHandshakeStatus,
    pid: Option<u32>,
    instance_id: Option<String>,
    detail: Option<String>,
}

enum Readiness {
    Foreground,
    Daemon { nonce: String },
}

async fn execute_command(command: ResolvedCommand) -> Result<ExitCode, String> {
    match command {
        ResolvedCommand::Check { project_root } => run_check(project_root),
        ResolvedCommand::Reload {
            project_root,
            state_dir,
        } => run_reload(project_root, state_dir).await,
        ResolvedCommand::Up {
            mode,
            project_root,
            state_dir,
        } => match mode {
            UpMode::Foreground => {
                run_owner(project_root, state_dir, mode, Readiness::Foreground).await
            }
            UpMode::Detached => start_detached(project_root, state_dir).await,
        },
        ResolvedCommand::Down { state_dir } => run_down(state_dir).await,
        ResolvedCommand::Status { state_dir, output } => run_status(state_dir, output).await,
    }
}

fn run_check(project_root: PathBuf) -> Result<ExitCode, String> {
    let project = Project::open(&project_root).map_err(|error| error.to_string())?;
    let revision = project.revision().map_err(|error| error.to_string())?;
    println!("Project check passed.");
    println!("Project: {}", project_root.display());
    print_agents(&revision.status());
    Ok(ExitCode::SUCCESS)
}

async fn run_reload(project_root: PathBuf, state_dir: PathBuf) -> Result<ExitCode, String> {
    let paths = RuntimePaths::new(state_dir);
    let report = probe_runtime(&paths).await;
    match report.status {
        DaemonStatus::Running => {
            if report.project_root.as_deref() != Some(project_root.as_path()) {
                return Err("the running runtime belongs to a different project root".to_owned());
            }
            let instance_id = report
                .instance_id
                .as_deref()
                .ok_or_else(|| "running metadata has no instance id".to_owned())?;
            let mut client = ControlClient::connect(paths.socket()).await?;
            client.verify_instance(instance_id)?;
            let project = client.reload_project().await?;
            println!("Project reloaded. The runtime now uses this revision.");
            print_project_summary(&project_root, &project);
            Ok(ExitCode::SUCCESS)
        }
        DaemonStatus::Stopped | DaemonStatus::Stale => {
            let project = Project::open(&project_root).map_err(|error| error.to_string())?;
            let revision = project.revision().map_err(|error| error.to_string())?;
            let lease = RuntimeLease::acquire(paths.clone()).map_err(|error| error.to_string())?;
            revision
                .write(paths.active_project())
                .map_err(|error| error.to_string())?;
            drop(lease);
            println!("Project reloaded. Start the runtime to use this revision.");
            print_project_summary(&project_root, &revision.status());
            println!("Next: zeta up --project-root {}", project_root.display());
            Ok(ExitCode::SUCCESS)
        }
        DaemonStatus::Stopping => Err("the runtime is stopping".to_owned()),
        DaemonStatus::Degraded => Err(report
            .detail
            .unwrap_or_else(|| "the runtime is degraded".to_owned())),
    }
}

async fn run_status(state_dir: PathBuf, output: StatusOutput) -> Result<ExitCode, String> {
    let paths = RuntimePaths::new(state_dir);
    let mut report = probe_runtime(&paths).await;
    if report.status == DaemonStatus::Running {
        let query = async {
            let instance_id = report
                .instance_id
                .as_deref()
                .ok_or_else(|| "running metadata has no instance id".to_owned())?;
            let mut client = ControlClient::connect(paths.socket()).await?;
            client.verify_instance(instance_id)?;
            let project = client.active_project().await?;
            let dispatch = client.runtime_status().await?;
            Ok((project, dispatch))
        }
        .await;
        match query {
            Ok((project, dispatch)) => {
                report.active_project = Some(project);
                report.dispatch = Some(dispatch);
            }
            Err(error) => {
                report.status = DaemonStatus::Degraded;
                report.detail = Some(error);
            }
        }
    } else {
        match ProjectRevision::read(paths.active_project()) {
            Ok(Some(project)) => {
                report.project_root = Some(project.project_root().to_path_buf());
                report.active_project = Some(project.status());
            }
            Ok(None) => {}
            Err(error) => {
                report.status = DaemonStatus::Degraded;
                report.detail = Some(error.to_string());
            }
        }
    }
    match output {
        StatusOutput::Human => print_human_status(&report),
        StatusOutput::Json => {
            let serialized = serde_json::to_string(&report).map_err(|error| error.to_string())?;
            println!("{serialized}");
        }
    }
    if report.status == DaemonStatus::Running {
        Ok(ExitCode::SUCCESS)
    } else {
        Ok(ExitCode::from(1))
    }
}

fn print_human_status(report: &StatusReport) {
    println!("Runtime: {}", status_name(report.status));
    if let Some(pid) = report.pid {
        println!("PID: {pid}");
    }
    if let Some(project_root) = &report.project_root {
        println!("Project: {}", project_root.display());
    }
    if let Some(project) = &report.active_project {
        print_active_project_summary(project);
    }
    if let Some(detail) = &report.detail {
        println!("Detail: {detail}");
    }
    if let Some(dispatch) = &report.dispatch {
        println!(
            "Agent lane: {}/{} active",
            dispatch.active_agents, dispatch.agent_capacity
        );
        println!(
            "Egress lane: {}/{} active, {} ready",
            dispatch.active_egress, dispatch.egress_capacity, dispatch.pending_egress
        );
        let queue = dispatch
            .queue
            .iter()
            .map(|(status, count)| format!("{status}={count}"))
            .collect::<Vec<_>>()
            .join(" ");
        let queue = if queue.is_empty() { "no work" } else { &queue };
        println!("Queue: {queue}");
        if let Some(deadline) = dispatch.next_deadline_ms {
            println!("Next deadline: {}", format_timestamp(deadline));
        }
    }
    if report.status == DaemonStatus::Stopped {
        println!("Next: zeta up --project-root <PROJECT_ROOT>");
    }
}

fn format_timestamp(timestamp_ms: i64) -> String {
    chrono::DateTime::from_timestamp_millis(timestamp_ms)
        .map(|timestamp: chrono::DateTime<chrono::Utc>| {
            timestamp.format("%Y-%m-%d %H:%M:%S UTC").to_string()
        })
        .unwrap_or_else(|| timestamp_ms.to_string())
}

fn print_project_summary(project_root: &Path, project: &ActiveProjectStatus) {
    println!("Project: {}", project_root.display());
    print_active_project_summary(project);
}

fn print_active_project_summary(project: &ActiveProjectStatus) {
    println!("Revision: {}", project.revision_id);
    print_agents(project);
}

fn print_agents(project: &ActiveProjectStatus) {
    println!("Agents: {}", project.agents.len());
    for agent in &project.agents {
        println!("  - {}: {}", agent.slug, agent.name);
    }
}

fn status_name(status: DaemonStatus) -> &'static str {
    match status {
        DaemonStatus::Running => "running",
        DaemonStatus::Stopping => "stopping",
        DaemonStatus::Stopped => "stopped",
        DaemonStatus::Degraded => "degraded",
        DaemonStatus::Stale => "stale",
    }
}

async fn probe_runtime(paths: &RuntimePaths) -> StatusReport {
    let state_metadata = match fs::symlink_metadata(paths.state_dir()) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == io::ErrorKind::NotFound => {
            return StatusReport::without_metadata(DaemonStatus::Stopped, paths, None);
        }
        Err(error) => {
            return StatusReport::without_metadata(
                DaemonStatus::Degraded,
                paths,
                Some(error.to_string()),
            );
        }
    };
    if !state_metadata.is_dir() && !state_metadata.file_type().is_symlink() {
        return StatusReport::without_metadata(
            DaemonStatus::Stale,
            paths,
            Some("the runtime state path is not a directory".to_owned()),
        );
    }
    let lease = match probe_lease(paths) {
        Ok(lease) => lease,
        Err(error) => {
            return StatusReport::without_metadata(DaemonStatus::Degraded, paths, Some(error));
        }
    };
    if lease == LeaseProbe::Free {
        let has_artifacts = [paths.metadata(), paths.socket(), paths.control_socket()]
            .into_iter()
            .any(|path| fs::symlink_metadata(path).is_ok());
        let status = if has_artifacts {
            DaemonStatus::Stale
        } else {
            DaemonStatus::Stopped
        };
        return StatusReport::without_metadata(status, paths, None);
    }
    let metadata = match RuntimeMetadata::read(paths) {
        Ok(Some(metadata)) => metadata,
        Ok(None) => {
            return StatusReport::without_metadata(
                DaemonStatus::Degraded,
                paths,
                Some("the runtime lease is held without owner metadata".to_owned()),
            );
        }
        Err(error) => {
            return StatusReport::without_metadata(
                DaemonStatus::Degraded,
                paths,
                Some(error.to_string()),
            );
        }
    };
    if metadata.schema_version != 1 || uuid::Uuid::parse_str(&metadata.instance_id).is_err() {
        return StatusReport::from_metadata(
            DaemonStatus::Degraded,
            paths,
            metadata,
            Some("owner metadata has an unsupported identity or schema".to_owned()),
        );
    }
    if metadata.state_dir != paths.state_dir()
        || metadata.socket != paths.socket()
        || metadata.control_socket != paths.control_socket()
    {
        return StatusReport::from_metadata(
            DaemonStatus::Degraded,
            paths,
            metadata,
            Some("owner metadata does not match the resolved lifecycle paths".to_owned()),
        );
    }
    if metadata.phase == RuntimePhase::Stopping {
        return StatusReport::from_metadata(DaemonStatus::Stopping, paths, metadata, None);
    }
    let health = tokio::time::timeout(
        Duration::from_secs(1),
        control_health(paths.control_socket(), &metadata.instance_id),
    )
    .await;
    match health {
        Ok(Ok(())) => StatusReport::from_metadata(DaemonStatus::Running, paths, metadata, None),
        Ok(Err(error)) => {
            StatusReport::from_metadata(DaemonStatus::Degraded, paths, metadata, Some(error))
        }
        Err(_elapsed) => StatusReport::from_metadata(
            DaemonStatus::Degraded,
            paths,
            metadata,
            Some("the control endpoint did not answer in time".to_owned()),
        ),
    }
}

fn probe_lease(paths: &RuntimePaths) -> Result<LeaseProbe, String> {
    let metadata = match fs::symlink_metadata(paths.lock()) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(LeaseProbe::Free),
        Err(error) => {
            return Err(format!(
                "cannot inspect '{}': {error}",
                paths.lock().display()
            ))
        }
    };
    if !metadata.file_type().is_file() {
        return Err(format!(
            "the runtime lease path is not a regular file: {}",
            paths.lock().display()
        ));
    }
    let descriptor = open(
        paths.lock(),
        OFlags::RDONLY | OFlags::CLOEXEC | OFlags::NOFOLLOW,
        Mode::empty(),
    )
    .map_err(|error| format!("cannot open '{}': {error}", paths.lock().display()))?;
    let file = File::from(descriptor);
    match flock(&file, FlockOperation::NonBlockingLockExclusive) {
        Ok(()) => Ok(LeaseProbe::Free),
        Err(rustix::io::Errno::WOULDBLOCK) => Ok(LeaseProbe::Held),
        Err(error) => Err(format!(
            "cannot inspect '{}': {error}",
            paths.lock().display()
        )),
    }
}

async fn run_down(state_dir: PathBuf) -> Result<ExitCode, String> {
    let paths = RuntimePaths::new(state_dir);
    let report = probe_runtime(&paths).await;
    match report.status {
        DaemonStatus::Stopped => {
            println!("Runtime is stopped.");
            Ok(ExitCode::SUCCESS)
        }
        DaemonStatus::Stale => {
            clean_stale_runtime(paths)?;
            println!("Runtime is stopped.");
            Ok(ExitCode::SUCCESS)
        }
        DaemonStatus::Running => {
            let instance_id = report
                .instance_id
                .as_deref()
                .ok_or_else(|| "running metadata has no instance id".to_owned())?;
            let mut client = ControlClient::connect(paths.control_socket()).await?;
            client.verify_instance(instance_id)?;
            client.shutdown().await?;
            wait_until_stopped(&paths).await?;
            println!("Runtime is stopped.");
            Ok(ExitCode::SUCCESS)
        }
        DaemonStatus::Stopping => {
            wait_until_stopped(&paths).await?;
            println!("Runtime is stopped.");
            Ok(ExitCode::SUCCESS)
        }
        DaemonStatus::Degraded => Err(report
            .detail
            .unwrap_or_else(|| "the runtime is degraded".to_owned())),
    }
}

fn clean_stale_runtime(paths: RuntimePaths) -> Result<(), String> {
    if !paths.state_dir().exists() {
        return Ok(());
    }
    let lease = RuntimeLease::acquire(paths.clone()).map_err(|error| error.to_string())?;
    lease
        .reconcile_stale_sockets()
        .map_err(|error| error.to_string())?;
    match RuntimeMetadata::read(&paths) {
        Ok(Some(metadata)) => {
            lease
                .remove_metadata(&metadata.instance_id)
                .map_err(|error| error.to_string())?;
        }
        Ok(None) => {}
        Err(_error) => {
            lease
                .remove_stale_metadata()
                .map_err(|cleanup| cleanup.to_string())?;
        }
    }
    Ok(())
}

async fn wait_until_stopped(paths: &RuntimePaths) -> Result<(), String> {
    let deadline = tokio::time::Instant::now() + Duration::from_secs(5);
    loop {
        if probe_lease(paths)? == LeaseProbe::Free {
            let report = probe_runtime(paths).await;
            if report.status == DaemonStatus::Stale {
                clean_stale_runtime(paths.clone())?;
            }
            return Ok(());
        }
        if tokio::time::Instant::now() >= deadline {
            return Err("the runtime did not release its ownership lease in time".to_owned());
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
}

struct ApplicationState {
    project_root: PathBuf,
    active_project_path: PathBuf,
    revision: Mutex<ProjectRevision>,
    runtime: Runtime,
}

impl ApplicationState {
    fn new(
        project_root: PathBuf,
        active_project_path: PathBuf,
        revision: ProjectRevision,
        runtime: Runtime,
    ) -> Self {
        Self {
            project_root,
            active_project_path,
            revision: Mutex::new(revision),
            runtime,
        }
    }

    fn handle(&self, request: Request) -> Result<Value, ErrorObject> {
        match request.method.as_str() {
            "agents.list" => self.empty_request(request, Self::active_project),
            "project.reload" => self.empty_request(request, Self::reload),
            "events.publish" => self.publish(request.params),
            "runtime.status" => self.runtime_status(),
            _ => Err(ErrorObject::new(METHOD_NOT_FOUND, "Method not found", None)),
        }
    }

    fn empty_request(
        &self,
        request: Request,
        handler: fn(&Self) -> Result<Value, ErrorObject>,
    ) -> Result<Value, ErrorObject> {
        if !request.params.is_empty() {
            return Err(ErrorObject::protocol(
                INVALID_PARAMS,
                "unexpected_parameters",
                format!("method {:?} does not accept parameters", request.method),
            ));
        }
        handler(self)
    }

    fn active_project(&self) -> Result<Value, ErrorObject> {
        let revision = self.revision.lock().map_err(|_error| {
            ErrorObject::application(
                SERVER_ERROR,
                "project_state_poisoned",
                "The active project state is unavailable",
                Retryability::Final,
            )
        })?;
        serde_json::to_value(revision.status()).map_err(|error| {
            ErrorObject::application(
                SERVER_ERROR,
                "project_status_encoding_failed",
                error.to_string(),
                Retryability::Final,
            )
        })
    }

    fn reload(&self) -> Result<Value, ErrorObject> {
        let project = Project::open(&self.project_root).map_err(|error| {
            ErrorObject::application(
                SERVER_ERROR,
                "project_reload_rejected",
                error.to_string(),
                Retryability::Final,
            )
        })?;
        let replacement = project.revision().map_err(|error| {
            ErrorObject::application(
                SERVER_ERROR,
                "project_reload_rejected",
                error.to_string(),
                Retryability::Final,
            )
        })?;
        replacement.routes().map_err(|error| {
            ErrorObject::application(
                SERVER_ERROR,
                "project_runtime_reload_failed",
                error.to_string(),
                Retryability::Final,
            )
        })?;
        let mut revision = self.revision.lock().map_err(|_error| {
            ErrorObject::application(
                SERVER_ERROR,
                "project_state_poisoned",
                "The active project state is unavailable",
                Retryability::Final,
            )
        })?;
        replacement
            .write(&self.active_project_path)
            .map_err(|error| {
                ErrorObject::application(
                    SERVER_ERROR,
                    "project_activation_failed",
                    error.to_string(),
                    Retryability::Final,
                )
            })?;
        self.runtime.reload(replacement.clone()).map_err(|error| {
            ErrorObject::application(
                SERVER_ERROR,
                "project_runtime_reload_failed",
                error.to_string(),
                Retryability::Final,
            )
        })?;
        *revision = replacement;
        serde_json::to_value(revision.status()).map_err(|error| {
            ErrorObject::application(
                SERVER_ERROR,
                "project_status_encoding_failed",
                error.to_string(),
                Retryability::Final,
            )
        })
    }

    fn publish(&self, params: Map<String, Value>) -> Result<Value, ErrorObject> {
        // Serialize ingress with reload. The actor then observes either the
        // previous revision or the complete replacement revision.
        let _revision = self.revision.lock().map_err(|_error| {
            ErrorObject::application(
                SERVER_ERROR,
                "project_runtime_unavailable",
                "the active project revision is unavailable",
                Retryability::Final,
            )
        })?;
        let event_type = params
            .get("type")
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty())
            .ok_or_else(|| invalid_application_params("type"))?;
        let payload = params
            .get("payload")
            .and_then(Value::as_object)
            .ok_or_else(|| invalid_application_params("payload"))?
            .clone();
        let optional_string = |field: &str| -> Result<Option<String>, ErrorObject> {
            match params.get(field) {
                None | Some(Value::Null) => Ok(None),
                Some(Value::String(value)) if !value.is_empty() => Ok(Some(value.clone())),
                Some(_) => Err(invalid_application_params(field)),
            }
        };
        let result = self
            .runtime
            .ingest(DraftEvent {
                event_type: event_type.to_owned(),
                source: "zeta:ipc".to_owned(),
                payload,
                idempotency_key: optional_string("idempotency_key")?,
                caused_by: optional_string("caused_by")?,
                session_id: optional_string("session_id")?,
                run_id: optional_string("run_id")?,
                turn_id: optional_string("turn_id")?,
            })
            .map_err(|error| {
                ErrorObject::application(
                    SERVER_ERROR,
                    "ingress_rejected",
                    error.to_string(),
                    Retryability::Final,
                )
            })?;
        serde_json::to_value(result).map_err(|error| {
            ErrorObject::application(
                SERVER_ERROR,
                "ingress_encoding_failed",
                error.to_string(),
                Retryability::Final,
            )
        })
    }

    fn runtime_status(&self) -> Result<Value, ErrorObject> {
        let status = self.runtime.status().map_err(|error| {
            ErrorObject::application(
                SERVER_ERROR,
                "dispatch_status_unavailable",
                error.to_string(),
                Retryability::Final,
            )
        })?;
        serde_json::to_value(status).map_err(|error| {
            ErrorObject::application(
                SERVER_ERROR,
                "dispatch_status_encoding_failed",
                error.to_string(),
                Retryability::Final,
            )
        })
    }

    fn shutdown(&self) -> Result<(), String> {
        self.runtime.shutdown().map_err(|error| error.to_string())
    }
}

fn invalid_application_params(field: &str) -> ErrorObject {
    ErrorObject::protocol(
        INVALID_PARAMS,
        "invalid_parameters",
        format!("parameter {field:?} is invalid"),
    )
}

async fn run_owner(
    project_root: PathBuf,
    state_dir: PathBuf,
    mode: UpMode,
    readiness: Readiness,
) -> Result<ExitCode, String> {
    let paths = RuntimePaths::new(state_dir);
    let lease = match RuntimeLease::acquire(paths.clone()) {
        Ok(lease) => lease,
        Err(error) if error.kind() == RuntimeLifecycleErrorKind::LeaseHeld => {
            let report = probe_runtime(&paths).await;
            if report.status != DaemonStatus::Running {
                return Err(report
                    .detail
                    .unwrap_or_else(|| format!("the runtime is {}", status_name(report.status))));
            }
            let pid = report
                .pid
                .ok_or_else(|| "running metadata has no process id".to_owned())?;
            announce_already_running(&readiness, pid, report.instance_id.as_deref())?;
            return Ok(ExitCode::SUCCESS);
        }
        Err(error) => return Err(error.to_string()),
    };
    lease
        .reconcile_stale_sockets()
        .map_err(|error| error.to_string())?;
    let revision = ProjectRevision::read(paths.active_project())
        .map_err(|error| error.to_string())?
        .ok_or_else(|| "no active project revision; run zeta reload first".to_owned())?;
    if revision.project_root() != project_root.as_path() {
        return Err("the active project revision belongs to a different project root".to_owned());
    }
    let runtime =
        Runtime::start(paths.dispatch(), revision.clone()).map_err(|error| error.to_string())?;
    let application_state = Arc::new(ApplicationState::new(
        project_root.clone(),
        paths.active_project().to_path_buf(),
        revision,
        runtime,
    ));
    if matches!(readiness, Readiness::Daemon { .. }) {
        redirect_daemon_stderr(&paths)?;
    }

    let instance_id = uuid::Uuid::new_v4().to_string();
    let started_at_ms = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|error| error.to_string())?
        .as_millis()
        .try_into()
        .map_err(|_error| "the current time does not fit runtime metadata".to_owned())?;
    let request_state = Arc::clone(&application_state);
    let application = LocalSocketServer::bind(
        paths.socket(),
        LocalSocketConfig::application(&instance_id),
        move |request| request_state.handle(request),
    )
    .await
    .map_err(|error| error.to_string())?;
    let control = match LocalSocketServer::bind(
        paths.control_socket(),
        LocalSocketConfig::control(&instance_id),
        |_request| unreachable!("control requests stay inside the IPC session"),
    )
    .await
    {
        Ok(control) => control,
        Err(error) => {
            application
                .shutdown()
                .await
                .map_err(|shutdown| format!("{error}; cleanup failed: {shutdown}"))?;
            return Err(error.to_string());
        }
    };
    let mut shutdown_signals = match ShutdownSignals::new() {
        Ok(signals) => signals,
        Err(error) => {
            let control_cleanup = control.shutdown().await;
            let application_cleanup = application.shutdown().await;
            if let Err(cleanup) = control_cleanup.and(application_cleanup) {
                return Err(format!("{error}; cleanup failed: {cleanup}"));
            }
            return Err(error);
        }
    };
    let owner_mode = match mode {
        UpMode::Foreground => RuntimeOwnerMode::Foreground,
        UpMode::Detached => RuntimeOwnerMode::Detached,
    };
    let mut metadata = RuntimeMetadata::new(
        &instance_id,
        std::process::id(),
        owner_mode,
        RuntimePhase::Running,
        project_root,
        &paths,
        started_at_ms,
    );
    if let Err(error) = lease.write_metadata(&metadata) {
        let control_cleanup = control.shutdown().await;
        let application_cleanup = application.shutdown().await;
        if let Err(cleanup) = control_cleanup.and(application_cleanup) {
            return Err(format!("{error}; cleanup failed: {cleanup}"));
        }
        return Err(error.to_string());
    }
    if let Err(error) = announce_ready(&readiness, metadata.pid, &instance_id) {
        let control_cleanup = control.shutdown().await;
        let application_cleanup = application.shutdown().await;
        let metadata_cleanup = lease.remove_metadata(&instance_id);
        if let Err(cleanup) = control_cleanup
            .and(application_cleanup)
            .map_err(|cleanup| cleanup.to_string())
            .and_then(|()| {
                metadata_cleanup
                    .map(|_removed| ())
                    .map_err(|cleanup| cleanup.to_string())
            })
        {
            return Err(format!("{error}; cleanup failed: {cleanup}"));
        }
        return Err(error);
    }

    let mut host_shutdown = control.host_shutdown();
    let wait_result = wait_for_shutdown(
        &application,
        &control,
        &mut host_shutdown,
        &mut shutdown_signals,
    )
    .await;
    metadata.phase = RuntimePhase::Stopping;
    let metadata_result = lease.write_metadata(&metadata);
    let control_result = control.shutdown().await;
    let application_result = application.shutdown().await;
    let reactive_result = application_state.shutdown();
    let remove_result = lease.remove_metadata(&instance_id);
    drop(lease);

    let shutdown_result = wait_result
        .and_then(|()| metadata_result.map_err(|error| error.to_string()))
        .and_then(|()| control_result.map_err(|error| error.to_string()))
        .and_then(|()| application_result.map_err(|error| error.to_string()))
        .and_then(|()| reactive_result)
        .and_then(|()| {
            remove_result
                .map(|_removed| ())
                .map_err(|error| error.to_string())
        });
    match shutdown_result {
        Ok(()) => Ok(ExitCode::SUCCESS),
        Err(error) if matches!(readiness, Readiness::Daemon { .. }) => {
            eprintln!("error: {error}");
            Ok(ExitCode::from(1))
        }
        Err(error) => Err(error),
    }
}

async fn wait_for_shutdown(
    application: &LocalSocketServer,
    control: &LocalSocketServer,
    host_shutdown: &mut tokio::sync::watch::Receiver<bool>,
    signals: &mut ShutdownSignals,
) -> Result<(), String> {
    let mut listener_check = tokio::time::interval(Duration::from_millis(100));
    loop {
        tokio::select! {
            changed = host_shutdown.changed() => {
                changed.map_err(|error| error.to_string())?;
                if *host_shutdown.borrow() {
                    return Ok(());
                }
            }
            signal = signals.terminate.recv() => {
                signal.ok_or_else(|| "the SIGTERM listener closed".to_owned())?;
                return Ok(());
            }
            signal = signals.interrupt.recv() => {
                signal.ok_or_else(|| "the SIGINT listener closed".to_owned())?;
                return Ok(());
            }
            signal = signals.hangup.recv() => {
                signal.ok_or_else(|| "the SIGHUP listener closed".to_owned())?;
                return Ok(());
            }
            _instant = listener_check.tick() => {
                if application.is_finished() || control.is_finished() {
                    return Err("a lifecycle socket listener stopped unexpectedly".to_owned());
                }
            }
        }
    }
}

struct ShutdownSignals {
    terminate: tokio::signal::unix::Signal,
    interrupt: tokio::signal::unix::Signal,
    hangup: tokio::signal::unix::Signal,
}

impl ShutdownSignals {
    fn new() -> Result<Self, String> {
        Ok(Self {
            terminate: tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
                .map_err(|error| error.to_string())?,
            interrupt: tokio::signal::unix::signal(tokio::signal::unix::SignalKind::interrupt())
                .map_err(|error| error.to_string())?,
            hangup: tokio::signal::unix::signal(tokio::signal::unix::SignalKind::hangup())
                .map_err(|error| error.to_string())?,
        })
    }
}

fn announce_ready(readiness: &Readiness, pid: u32, instance_id: &str) -> Result<(), String> {
    match readiness {
        Readiness::Foreground => {
            println!("Runtime is running.");
            println!("PID: {pid}");
            println!("Press Ctrl-C to stop.");
            io::stdout().flush().map_err(|error| error.to_string())?;
        }
        Readiness::Daemon { nonce } => write_handshake(DaemonHandshake {
            nonce: nonce.clone(),
            status: DaemonHandshakeStatus::Ready,
            pid: Some(pid),
            instance_id: Some(instance_id.to_owned()),
            detail: None,
        })?,
    }
    Ok(())
}

fn announce_already_running(
    readiness: &Readiness,
    pid: u32,
    instance_id: Option<&str>,
) -> Result<(), String> {
    match readiness {
        Readiness::Foreground => {
            println!("Runtime is already running.");
            println!("PID: {pid}");
            io::stdout().flush().map_err(|error| error.to_string())?;
        }
        Readiness::Daemon { nonce } => write_handshake(DaemonHandshake {
            nonce: nonce.clone(),
            status: DaemonHandshakeStatus::AlreadyRunning,
            pid: Some(pid),
            instance_id: instance_id.map(str::to_owned),
            detail: None,
        })?,
    }
    Ok(())
}

fn write_handshake(handshake: DaemonHandshake) -> Result<(), String> {
    let serialized = serde_json::to_string(&handshake).map_err(|error| error.to_string())?;
    let mut stdout = io::stdout().lock();
    writeln!(stdout, "{serialized}").map_err(|error| error.to_string())?;
    stdout.flush().map_err(|error| error.to_string())
}

fn redirect_daemon_stderr(paths: &RuntimePaths) -> Result<(), String> {
    let descriptor = open(
        paths.log(),
        OFlags::WRONLY | OFlags::CREATE | OFlags::APPEND | OFlags::CLOEXEC | OFlags::NOFOLLOW,
        Mode::RUSR | Mode::WUSR,
    )
    .map_err(|error| format!("cannot open '{}': {error}", paths.log().display()))?;
    let file = File::from(descriptor);
    file.set_permissions(fs::Permissions::from_mode(0o600))
        .map_err(|error| format!("cannot protect '{}': {error}", paths.log().display()))?;
    rustix::stdio::dup2_stderr(&file).map_err(|error| error.to_string())
}

async fn start_detached(project_root: PathBuf, state_dir: PathBuf) -> Result<ExitCode, String> {
    let nonce = uuid::Uuid::new_v4().to_string();
    let executable = env::current_exe().map_err(|error| error.to_string())?;
    let mut child = ProcessCommand::new(executable)
        .arg("--daemon-child")
        .arg(&nonce)
        .arg("--project-root")
        .arg(&project_root)
        .arg("--state-dir")
        .arg(&state_dir)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|error| format!("cannot start the detached runtime: {error}"))?;
    let stdout = child
        .stdout
        .take()
        .ok_or_else(|| "the detached runtime has no readiness channel".to_owned())?;
    let (sender, receiver) = mpsc::channel();
    std::thread::spawn(move || {
        let mut line = String::new();
        let result = StdBufReader::new(stdout)
            .read_line(&mut line)
            .map(|_count| line);
        let _sent = sender.send(result);
    });
    let line = match receiver.recv_timeout(Duration::from_secs(10)) {
        Ok(Ok(line)) => line,
        Ok(Err(error)) => {
            terminate_child(&mut child);
            return Err(format!("cannot read detached readiness: {error}"));
        }
        Err(error) => {
            terminate_child(&mut child);
            return Err(format!(
                "the detached runtime did not become ready: {error}"
            ));
        }
    };
    let handshake: DaemonHandshake = match serde_json::from_str(line.trim_end()) {
        Ok(handshake) => handshake,
        Err(error) => {
            terminate_child(&mut child);
            return Err(format!("invalid detached readiness handshake: {error}"));
        }
    };
    if handshake.nonce != nonce {
        terminate_child(&mut child);
        return Err("the detached readiness nonce did not match".to_owned());
    }
    match handshake.status {
        DaemonHandshakeStatus::Ready => {
            let pid = handshake
                .pid
                .ok_or_else(|| "the detached readiness handshake has no process id".to_owned())?;
            println!("Runtime started.");
            println!("PID: {pid}");
            println!("Use 'zeta status' to inspect the runtime.");
            Ok(ExitCode::SUCCESS)
        }
        DaemonHandshakeStatus::AlreadyRunning => {
            let _status = child.wait();
            let pid = handshake
                .pid
                .ok_or_else(|| "the detached readiness handshake has no process id".to_owned())?;
            println!("Runtime is already running.");
            println!("PID: {pid}");
            Ok(ExitCode::SUCCESS)
        }
        DaemonHandshakeStatus::Error => {
            let _status = child.wait();
            Err(handshake
                .detail
                .unwrap_or_else(|| "the detached runtime could not start".to_owned()))
        }
    }
}

fn terminate_child(child: &mut std::process::Child) {
    let _killed = child.kill();
    let _status = child.wait();
}

async fn run_daemon_child(arguments: &[OsString]) -> ExitCode {
    let parsed = parse_daemon_arguments(arguments);
    let (nonce, project_root, state_dir) = match parsed {
        Ok(parsed) => parsed,
        Err((nonce, error)) => {
            let _handshake = write_handshake(DaemonHandshake {
                nonce,
                status: DaemonHandshakeStatus::Error,
                pid: None,
                instance_id: None,
                detail: Some(error),
            });
            return ExitCode::from(1);
        }
    };
    if let Err(error) = rustix::process::setsid() {
        let _handshake = write_handshake(DaemonHandshake {
            nonce,
            status: DaemonHandshakeStatus::Error,
            pid: None,
            instance_id: None,
            detail: Some(error.to_string()),
        });
        return ExitCode::from(1);
    }
    match run_owner(
        project_root,
        state_dir,
        UpMode::Detached,
        Readiness::Daemon {
            nonce: nonce.clone(),
        },
    )
    .await
    {
        Ok(code) => code,
        Err(error) => {
            let _handshake = write_handshake(DaemonHandshake {
                nonce,
                status: DaemonHandshakeStatus::Error,
                pid: None,
                instance_id: None,
                detail: Some(error),
            });
            ExitCode::from(1)
        }
    }
}

fn parse_daemon_arguments(
    arguments: &[OsString],
) -> Result<(String, PathBuf, PathBuf), (String, String)> {
    let nonce = arguments
        .get(1)
        .map(|value| value.to_string_lossy().into_owned())
        .unwrap_or_default();
    if arguments.len() != 6
        || arguments.get(2) != Some(&OsString::from("--project-root"))
        || arguments.get(4) != Some(&OsString::from("--state-dir"))
    {
        return Err((nonce, "invalid detached child invocation".to_owned()));
    }
    let project_root = PathBuf::from(&arguments[3]);
    let state_dir = PathBuf::from(&arguments[5]);
    if !project_root.is_absolute() || !state_dir.is_absolute() {
        return Err((nonce, "detached child paths must be absolute".to_owned()));
    }
    Ok((nonce, project_root, state_dir))
}

struct ControlClient {
    session: Session,
    reader: BufReader<OwnedReadHalf>,
    writer: OwnedWriteHalf,
    next_id: u64,
}

impl ControlClient {
    async fn connect(path: &Path) -> Result<Self, String> {
        let stream = UnixStream::connect(path)
            .await
            .map_err(|error| format!("cannot connect to '{}': {error}", path.display()))?;
        let (reader, writer) = stream.into_split();
        let parameters = InitializeParams {
            protocol_versions: vec![PROTOCOL_VERSION],
            peer: PeerIdentity::new("zeta-cli", env!("CARGO_PKG_VERSION")),
            roles: vec![Role::Client],
            event_types: None,
            methods: None,
            heartbeat_seconds: Some(10.0),
            max_in_flight: Some(8),
        };
        let mut client = Self {
            session: Session::peer(parameters, ShutdownDirection::LocalSupervisesRemote),
            reader: BufReader::new(reader),
            writer,
            next_id: 1,
        };
        let actions = client
            .session
            .initialize(RequestId::from("initialize"))
            .map_err(|error| error.to_string())?;
        client.resolve(actions).await?;
        Ok(client)
    }

    fn verify_instance(&self, expected: &str) -> Result<(), String> {
        let result = self
            .session
            .initialization_result()
            .ok_or_else(|| "the control endpoint did not initialize".to_owned())?;
        let actual = result.config.get("instance_id").and_then(Value::as_str);
        if actual != Some(expected) {
            return Err("the control endpoint belongs to a different runtime instance".to_owned());
        }
        Ok(())
    }

    async fn ping(&mut self) -> Result<(), String> {
        let actions = self.request("ping", Map::new())?;
        self.resolve(actions).await.map(|_result| ())
    }

    async fn active_project(&mut self) -> Result<ActiveProjectStatus, String> {
        let actions = self.request("agents.list", Map::new())?;
        let response = self.resolve(actions).await?;
        serde_json::from_value(response)
            .map_err(|error| format!("the runtime returned an invalid agent list: {error}"))
    }

    async fn reload_project(&mut self) -> Result<ActiveProjectStatus, String> {
        let actions = self.request("project.reload", Map::new())?;
        let response = self.resolve(actions).await?;
        serde_json::from_value(response)
            .map_err(|error| format!("the runtime returned an invalid reload result: {error}"))
    }

    async fn runtime_status(&mut self) -> Result<RuntimeStatus, String> {
        let actions = self.request("runtime.status", Map::new())?;
        let response = self.resolve(actions).await?;
        serde_json::from_value(response)
            .map_err(|error| format!("the runtime returned invalid dispatch status: {error}"))
    }

    async fn shutdown(&mut self) -> Result<(), String> {
        let mut parameters = Map::new();
        parameters.insert("reason".to_owned(), Value::String("zeta down".to_owned()));
        let actions = self.request("shutdown", parameters)?;
        match self.resolve(actions).await {
            Ok(_result) => Ok(()),
            Err(error) if error == "the control endpoint closed before answering" => Ok(()),
            Err(error) => Err(error),
        }
    }

    fn request(
        &mut self,
        method: &str,
        parameters: Map<String, Value>,
    ) -> Result<Vec<Action>, String> {
        let id = RequestId::from(self.next_id);
        self.next_id = self.next_id.saturating_add(1);
        self.session
            .send_request(id, method, parameters)
            .map_err(|error| error.to_string())
    }

    async fn resolve(&mut self, actions: Vec<Action>) -> Result<Value, String> {
        let mut actions = std::collections::VecDeque::from(actions);
        loop {
            while let Some(action) = actions.pop_front() {
                match action {
                    Action::Send(message) => self.write_message(&message).await?,
                    Action::RequestResolved(resolved) => {
                        return resolved.outcome.map_err(|error| {
                            format!("control request failed ({}): {}", error.code, error.message)
                        });
                    }
                    Action::Violation(error) => return Err(error.to_string()),
                    Action::Close { reason } => {
                        return Err(
                            reason.unwrap_or_else(|| "the control endpoint closed".to_owned())
                        );
                    }
                    Action::HandleRequest(request) => {
                        return Err(format!("unexpected control request {:?}", request.method));
                    }
                    Action::HandleNotification(notification) => {
                        return Err(format!(
                            "unexpected control notification {:?}",
                            notification.method
                        ));
                    }
                }
            }
            let message = self.read_message().await?;
            for action in self.session.receive(message) {
                actions.push_back(action);
            }
        }
    }

    async fn write_message(&mut self, message: &Message) -> Result<(), String> {
        self.writer
            .write_all(message.to_json().as_bytes())
            .await
            .map_err(|error| error.to_string())?;
        self.writer
            .write_all(b"\n")
            .await
            .map_err(|error| error.to_string())?;
        self.writer.flush().await.map_err(|error| error.to_string())
    }

    async fn read_message(&mut self) -> Result<Message, String> {
        let mut line = Vec::new();
        loop {
            let buffer = self
                .reader
                .fill_buf()
                .await
                .map_err(|error| error.to_string())?;
            if buffer.is_empty() {
                return Err("the control endpoint closed before answering".to_owned());
            }
            let newline = buffer.iter().position(|byte| *byte == b'\n');
            let data_length = newline.unwrap_or(buffer.len());
            let remaining = MAX_FRAME_BYTES.saturating_sub(line.len());
            if data_length > remaining {
                return Err("the control endpoint sent an oversized frame".to_owned());
            }
            line.extend_from_slice(&buffer[..data_length]);
            let consumed = newline.map_or(buffer.len(), |index| index + 1);
            self.reader.consume(consumed);
            if newline.is_some() {
                break;
            }
        }
        let line = std::str::from_utf8(&line).map_err(|error| error.to_string())?;
        Message::parse_str(line).map_err(|error| error.to_string())
    }
}

async fn control_health(path: &Path, instance_id: &str) -> Result<(), String> {
    let mut client = ControlClient::connect(path).await?;
    client.verify_instance(instance_id)?;
    client.ping().await
}

#[cfg(test)]
mod tests {
    use std::fs;
    use std::os::unix::fs::symlink;
    use std::path::{Path, PathBuf};

    use tempfile::TempDir;

    use super::*;

    fn resolve(
        command: Command,
        invocation_dir: &Path,
        environment_state_dir: Option<&Path>,
        home: &Path,
    ) -> Result<ResolvedCommand, PathError> {
        resolve_command_paths(
            command,
            invocation_dir,
            environment_state_dir.map(Path::as_os_str),
            home,
        )
    }

    fn canonical(path: &Path) -> PathBuf {
        fs::canonicalize(path).expect("canonical test path")
    }

    #[test]
    fn clap_maps_the_public_command_contract() {
        let check = Cli::try_parse_from(["zeta", "check", "--project-root", "project"])
            .expect("check must parse");
        assert_eq!(
            Command::from(check.command),
            Command::Check {
                project_root: PathBuf::from("project"),
            }
        );

        let reload = Cli::try_parse_from([
            "zeta",
            "reload",
            "--project-root",
            "project",
            "--state-dir",
            "state",
        ])
        .expect("reload must parse");
        assert_eq!(
            Command::from(reload.command),
            Command::Reload {
                project_root: PathBuf::from("project"),
                state_dir: Some(PathBuf::from("state")),
            }
        );

        let up = Cli::try_parse_from([
            "zeta",
            "up",
            "--detach",
            "--project-root",
            "project",
            "--state-dir",
            "state",
        ])
        .expect("up must parse");
        assert_eq!(
            Command::from(up.command),
            Command::Up {
                mode: UpMode::Detached,
                project_root: PathBuf::from("project"),
                state_dir: Some(PathBuf::from("state")),
            }
        );

        let down =
            Cli::try_parse_from(["zeta", "down", "--state-dir", "state"]).expect("down must parse");
        assert_eq!(
            Command::from(down.command),
            Command::Down {
                project_root: None,
                state_dir: Some(PathBuf::from("state")),
            }
        );

        let status = Cli::try_parse_from(["zeta", "status", "--json"]).expect("status must parse");
        assert_eq!(
            Command::from(status.command),
            Command::Status {
                project_root: None,
                state_dir: None,
                output: StatusOutput::Json,
            }
        );
    }

    #[test]
    fn clap_rejects_legacy_and_malformed_forms() {
        for arguments in [
            &["zeta", "run"][..],
            &["zeta", "serve"][..],
            &["zeta", "help"][..],
            &["zeta", "up", "--json"][..],
            &["zeta", "up", "-d", "--detach"][..],
            &["zeta", "status", "--json", "--json"][..],
            &[
                "zeta",
                "down",
                "--state-dir",
                "first",
                "--state-dir",
                "second",
            ][..],
            &["zeta", "status", "extra"][..],
        ] {
            let error = Cli::try_parse_from(arguments).expect_err("the invocation must fail");
            assert_eq!(error.exit_code(), 2);
        }
    }

    #[test]
    fn clap_help_explains_the_project_lifecycle() {
        let help = Cli::command().render_long_help().to_string();
        assert!(help.contains("GET STARTED:"));
        assert!(help.contains("zeta reload --project-root ~/my-project"));
        assert!(help.contains("zeta <command> --help"));

        let reload = Cli::command()
            .find_subcommand("reload")
            .expect("reload command must exist")
            .clone()
            .render_long_help()
            .to_string();
        assert!(reload.contains("A failed reload keeps the prior revision active."));
        assert!(reload.contains("zeta reload --project-root ~/my-project"));
    }

    #[test]
    fn paths_use_explicit_then_environment_precedence() {
        let temp = TempDir::new().expect("temporary directory");
        let invocation_dir = temp.path().join("invocation");
        let project = temp.path().join("project");
        fs::create_dir_all(&invocation_dir).expect("invocation directory");
        fs::create_dir_all(&project).expect("project directory");

        let explicit = resolve(
            Command::Up {
                mode: UpMode::Foreground,
                project_root: project.clone(),
                state_dir: Some(PathBuf::from("explicit")),
            },
            &invocation_dir,
            Some(Path::new("environment")),
            temp.path(),
        )
        .expect("explicit state path");
        assert_eq!(
            explicit.state_dir(),
            canonical(&invocation_dir).join("explicit")
        );

        let environment = resolve(
            Command::Status {
                project_root: None,
                state_dir: None,
                output: StatusOutput::Human,
            },
            &invocation_dir,
            Some(Path::new("environment")),
            temp.path(),
        )
        .expect("environment state path");
        assert_eq!(
            environment.state_dir(),
            canonical(&invocation_dir).join("environment")
        );

        let reload = resolve(
            Command::Reload {
                project_root: project,
                state_dir: None,
            },
            &invocation_dir,
            Some(Path::new("environment")),
            temp.path(),
        )
        .expect("reload state path");
        assert_eq!(
            reload.state_dir(),
            canonical(&invocation_dir).join("environment")
        );
    }

    fn write_agent(root: &Path, slug: &str, description: &str) {
        let agents = root.join("agents");
        fs::create_dir_all(&agents).expect("agents directory");
        fs::write(
            agents.join(format!("{slug}.md")),
            format!("---\nname: {slug}\ndescription: {description}\n---\nReport.\n"),
        )
        .expect("agent source");
    }

    #[test]
    fn application_state_lists_the_deployed_revision_until_reload() {
        let temporary = TempDir::new().expect("temporary directory");
        write_agent(temporary.path(), "alpha", "Reports alpha.");
        let active = Project::open(temporary.path())
            .expect("active project")
            .revision()
            .expect("active revision");
        let state_directory = temporary.path().join("state");
        fs::create_dir(&state_directory).expect("state directory");
        let active_path = state_directory.join("active-project.json");
        active.write(&active_path).expect("active project write");
        let runtime = Runtime::start(state_directory.join("zeta.sqlite3"), active.clone())
            .expect("runtime runtime");
        let application = ApplicationState::new(
            fs::canonicalize(temporary.path()).expect("project root"),
            active_path,
            active,
            runtime,
        );

        write_agent(temporary.path(), "bravo", "Reports bravo.");
        let listed = application
            .handle(Request::new(1_u64.into(), "agents.list", Map::new()))
            .expect("agent list");
        let listed: ActiveProjectStatus = serde_json::from_value(listed).expect("agent status");
        assert_eq!(
            listed
                .agents
                .iter()
                .map(|agent| agent.slug.as_str())
                .collect::<Vec<_>>(),
            vec!["alpha"]
        );

        let reloaded = application
            .handle(Request::new(2_u64.into(), "project.reload", Map::new()))
            .expect("reload");
        let reloaded: ActiveProjectStatus =
            serde_json::from_value(reloaded).expect("reload status");
        assert_eq!(
            reloaded
                .agents
                .iter()
                .map(|agent| agent.slug.as_str())
                .collect::<Vec<_>>(),
            vec!["alpha", "bravo"]
        );
    }

    #[test]
    fn paths_discover_the_nearest_marker_upward() {
        let temp = TempDir::new().expect("temporary directory");
        let project = temp.path().join("project");
        let nested = project.join("src/package");
        let outer = project.join(".zeta");
        let inner = project.join("src/.zeta");
        fs::create_dir_all(&nested).expect("nested project directory");
        fs::create_dir(&outer).expect("outer state marker");
        fs::create_dir(&inner).expect("inner state marker");

        let resolved = resolve(
            Command::Up {
                mode: UpMode::Foreground,
                project_root: nested,
                state_dir: None,
            },
            temp.path(),
            None,
            temp.path(),
        )
        .expect("discovered state path");

        assert_eq!(
            resolved.state_dir(),
            canonical(&project.join("src")).join(".zeta")
        );
    }

    #[test]
    fn down_and_status_discover_from_the_invocation_directory() {
        let temp = TempDir::new().expect("temporary directory");
        let project = temp.path().join("project");
        let nested = project.join("src/package");
        let marker = project.join(".zeta");
        fs::create_dir_all(&nested).expect("nested directory");
        fs::create_dir(&marker).expect("state marker");

        for command in [
            Command::Down {
                project_root: None,
                state_dir: None,
            },
            Command::Status {
                project_root: None,
                state_dir: None,
                output: StatusOutput::Json,
            },
        ] {
            let resolved =
                resolve(command, &nested, None, temp.path()).expect("discovered state path");
            assert_eq!(resolved.state_dir(), canonical(&project).join(".zeta"));
        }
    }

    #[test]
    fn down_and_status_accept_an_explicit_project_root() {
        let temp = TempDir::new().expect("temporary directory");
        let invocation = temp.path().join("invocation");
        let project = temp.path().join("project");
        fs::create_dir(&invocation).expect("invocation directory");
        fs::create_dir(&project).expect("project directory");
        fs::create_dir(project.join(".zeta")).expect("state marker");

        for command in [
            Command::Down {
                project_root: Some(project.clone()),
                state_dir: None,
            },
            Command::Status {
                project_root: Some(project.clone()),
                state_dir: None,
                output: StatusOutput::Human,
            },
        ] {
            let resolved =
                resolve(command, &invocation, None, temp.path()).expect("project state path");
            assert_eq!(resolved.state_dir(), canonical(&project).join(".zeta"));
        }
    }

    #[test]
    fn paths_exclude_the_home_marker_for_descendants() {
        let temp = TempDir::new().expect("temporary directory");
        let home = temp.path().join("home");
        let project = home.join("projects/zeta");
        fs::create_dir_all(&project).expect("project directory");
        fs::create_dir(home.join(".zeta")).expect("home marker");

        let resolved = resolve(
            Command::Status {
                project_root: None,
                state_dir: None,
                output: StatusOutput::Human,
            },
            &project,
            None,
            &home,
        )
        .expect("fallback state path");

        assert_eq!(resolved.state_dir(), canonical(&project).join(".zeta"));
    }

    #[test]
    fn paths_accept_a_directory_symlink_marker() {
        let temp = TempDir::new().expect("temporary directory");
        let project = temp.path().join("project");
        let state = temp.path().join("state");
        fs::create_dir(&project).expect("project directory");
        fs::create_dir(&state).expect("state directory");
        symlink(&state, project.join(".zeta")).expect("state symlink");

        let resolved = resolve(
            Command::Status {
                project_root: None,
                state_dir: None,
                output: StatusOutput::Human,
            },
            &project,
            None,
            temp.path(),
        )
        .expect("symlink state marker");

        assert_eq!(resolved.state_dir(), canonical(&state));
    }

    #[test]
    fn paths_reject_invalid_markers() {
        let temp = TempDir::new().expect("temporary directory");
        let regular = temp.path().join("regular");
        let broken = temp.path().join("broken");
        fs::create_dir(&regular).expect("regular project directory");
        fs::create_dir(&broken).expect("broken project directory");
        fs::write(regular.join(".zeta"), "not a directory").expect("regular marker");
        symlink(temp.path().join("missing"), broken.join(".zeta")).expect("broken marker symlink");

        for project in [regular, broken] {
            let error = resolve(
                Command::Status {
                    project_root: None,
                    state_dir: None,
                    output: StatusOutput::Human,
                },
                &project,
                None,
                temp.path(),
            )
            .expect_err("the marker must be rejected");
            assert!(error
                .to_string()
                .starts_with("runtime state marker is not a directory:"));
        }
    }

    #[test]
    fn status_and_down_path_resolution_do_not_create_state() {
        let temp = TempDir::new().expect("temporary directory");
        let project = temp.path().join("project");
        fs::create_dir(&project).expect("project directory");

        for command in [
            Command::Down {
                project_root: None,
                state_dir: None,
            },
            Command::Status {
                project_root: None,
                state_dir: None,
                output: StatusOutput::Human,
            },
        ] {
            let resolved =
                resolve(command, &project, None, temp.path()).expect("fallback state path");
            assert_eq!(resolved.state_dir(), canonical(&project).join(".zeta"));
            assert!(!project.join(".zeta").exists());
        }
    }

    #[test]
    fn up_requires_an_existing_project_directory() {
        let temp = TempDir::new().expect("temporary directory");
        let missing = temp.path().join("missing");
        let file = temp.path().join("file");
        fs::write(&file, "not a directory").expect("project file");

        for project_root in [missing, file] {
            let error = resolve(
                Command::Up {
                    mode: UpMode::Foreground,
                    project_root,
                    state_dir: None,
                },
                temp.path(),
                None,
                temp.path(),
            )
            .expect_err("the project root must be rejected");
            assert!(error
                .to_string()
                .starts_with("project root is not a directory:"));
        }
    }
}
