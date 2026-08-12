//! Executes the native capabilities owned by one agent invocation.

mod bash;
mod declarations;
mod edit;
mod files;
mod search;
mod web;

use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

use serde_json::{json, Map, Value};
use zeta_substrate::hash_bytes;

use crate::{AbortSignal, AgentError, CapabilityInvocation, ToolExecutor, ToolFuture};

pub use declarations::native_capabilities;
pub use files::{HttpFuture, HttpResponse, HttpTransport, UnavailableHttpTransport};
pub use search::{CommandOutput, CommandRunner, SystemCommandRunner};
pub use web::{
    UnavailableWebSearch, WebSearchFuture, WebSearchProvider, WebSearchResult, WebSearchSource,
};

static NEXT_ARTIFACT_ID: AtomicU64 = AtomicU64::new(0);

/// Executes native capabilities against an explicit invocation directory.
///
/// # Examples
///
/// ```
/// let executor = zeta_agent::NativeToolExecutor::default();
/// drop(executor);
/// ```
pub struct NativeToolExecutor<
    C = SystemCommandRunner,
    H = UnavailableHttpTransport,
    W = UnavailableWebSearch,
> {
    commands: C,
    http: H,
    web_search: W,
}

impl Default
    for NativeToolExecutor<SystemCommandRunner, UnavailableHttpTransport, UnavailableWebSearch>
{
    fn default() -> Self {
        NativeToolExecutor::new(SystemCommandRunner)
    }
}

impl<C> NativeToolExecutor<C, UnavailableHttpTransport, UnavailableWebSearch> {
    /// Creates an executor with one explicit subprocess boundary.
    ///
    /// # Examples
    ///
    /// ```
    /// let executor = zeta_agent::NativeToolExecutor::new(
    ///     zeta_agent::SystemCommandRunner,
    /// );
    /// drop(executor);
    /// ```
    pub fn new(commands: C) -> Self {
        NativeToolExecutor {
            commands,
            http: UnavailableHttpTransport,
            web_search: UnavailableWebSearch,
        }
    }
}

impl<C, H, W> NativeToolExecutor<C, H, W> {
    /// Creates an executor with explicit subprocess and network boundaries.
    ///
    /// # Examples
    ///
    /// ```
    /// let executor = zeta_agent::NativeToolExecutor::with_network(
    ///     zeta_agent::SystemCommandRunner,
    ///     zeta_agent::UnavailableHttpTransport,
    ///     zeta_agent::UnavailableWebSearch,
    /// );
    /// drop(executor);
    /// ```
    pub fn with_network(commands: C, http: H, web_search: W) -> Self {
        NativeToolExecutor {
            commands,
            http,
            web_search,
        }
    }
}

impl<C, H, W> ToolExecutor for NativeToolExecutor<C, H, W>
where
    C: CommandRunner,
    H: HttpTransport,
    W: WebSearchProvider,
{
    fn execute<'a>(
        &'a mut self,
        invocation: &'a CapabilityInvocation,
        abort: &'a dyn AbortSignal,
    ) -> ToolFuture<'a> {
        Box::pin(async move { self.execute_now(invocation, abort).await })
    }
}

impl<C, H, W> NativeToolExecutor<C, H, W>
where
    C: CommandRunner,
    H: HttpTransport,
    W: WebSearchProvider,
{
    async fn execute_now(
        &mut self,
        invocation: &CapabilityInvocation,
        abort: &dyn AbortSignal,
    ) -> Result<Map<String, Value>, AgentError> {
        check_abort(abort)?;
        let base_directory = invocation
            .base_directory
            .as_deref()
            .map(PathBuf::from)
            .unwrap_or_else(|| PathBuf::from("."));
        let id = invocation.capability_id.as_str();
        if id == "zeta.read" {
            let result =
                files::read(&invocation.params, &base_directory, &mut self.http, abort).await;
            check_abort(abort)?;
            return Ok(result);
        }
        if id == "zeta.write" {
            return Ok(files::write(&invocation.params, &base_directory));
        }
        if id == "zeta.ls" {
            return Ok(files::list(&invocation.params, &base_directory));
        }
        if id == "zeta.edit" {
            return Ok(edit::edit(&invocation.params, &base_directory));
        }
        if id == "zeta.patch" {
            return Ok(edit::patch(&invocation.params, &base_directory));
        }
        if id == "zeta.grep" {
            return search::grep(
                &invocation.params,
                &base_directory,
                &mut self.commands,
                abort,
            );
        }
        if id == "zeta.ast_grep" {
            return search::ast_grep(
                &invocation.params,
                &base_directory,
                &mut self.commands,
                abort,
            );
        }
        if id == "zeta.bash" {
            return bash::bash(
                &invocation.params,
                &base_directory,
                &mut self.commands,
                abort,
            );
        }
        if id == "zeta.web_search" {
            let result = web::web_search(&invocation.params, &mut self.web_search, abort).await;
            check_abort(abort)?;
            return Ok(result);
        }
        Ok(error_result(
            "unknown-native-tool",
            format!("unknown native capability: {id}"),
        ))
    }
}

fn check_abort(abort: &dyn AbortSignal) -> Result<(), AgentError> {
    let Some(reason) = abort.reason() else {
        return Ok(());
    };
    Err(AgentError::tool(format!(
        "tool execution aborted: {reason}"
    )))
}

/// Bounds text by preserving equal head and tail portions.
///
/// # Examples
///
/// ```
/// assert_eq!(
///     zeta_agent::bounded_output("012345", 4),
///     ("01\n... 2 characters truncated ...\n45".to_owned(), true),
/// );
/// ```
pub fn bounded_output(text: &str, limit: usize) -> (String, bool) {
    let characters: Vec<char> = text.chars().collect();
    if characters.len() <= limit {
        return (text.to_owned(), false);
    }
    let half = limit / 2;
    let head: String = characters[..half].iter().collect();
    let tail_start = characters.len().saturating_sub(half);
    let tail: String = characters[tail_start..].iter().collect();
    let omitted = characters.len() - head.chars().count() - tail.chars().count();
    (
        format!("{head}\n... {omitted} characters truncated ...\n{tail}"),
        true,
    )
}

fn resolve_path(path: &str, base_directory: &Path) -> PathBuf {
    let path = PathBuf::from(path);
    if path.is_absolute() {
        path
    } else {
        base_directory.join(path)
    }
}

fn content_hash(bytes: &[u8]) -> String {
    hash_bytes(bytes).to_string()
}

fn short_tag(hash: &str) -> &str {
    hash.strip_prefix("b3:")
        .and_then(|digest| digest.get(..8))
        .unwrap_or("")
}

fn error_result(code: &str, message: impl Into<String>) -> Map<String, Value> {
    object(json!({
        "ok": false,
        "error": {"code": code, "message": message.into()},
    }))
}

fn object(value: Value) -> Map<String, Value> {
    let Value::Object(value) = value else {
        unreachable!("tool result must be a JSON object")
    };
    value
}

fn string_param(params: &Map<String, Value>, name: &str) -> String {
    params
        .get(name)
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_owned()
}

fn integer_param(params: &Map<String, Value>, name: &str, default: usize) -> usize {
    params
        .get(name)
        .and_then(Value::as_u64)
        .and_then(|value| usize::try_from(value).ok())
        .unwrap_or(default)
}

fn write_artifact(prefix: &str, content: &str) -> Result<PathBuf, std::io::Error> {
    loop {
        let id = NEXT_ARTIFACT_ID.fetch_add(1, Ordering::Relaxed);
        let path = std::env::temp_dir().join(format!("{prefix}-{}-{id}.patch", std::process::id()));
        let opened = OpenOptions::new().write(true).create_new(true).open(&path);
        match opened {
            Ok(mut file) => {
                file.write_all(content.as_bytes())?;
                return Ok(path);
            }
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => continue,
            Err(error) => return Err(error),
        }
    }
}

fn change_hashes(path: &Path, content: &str) -> Map<String, Value> {
    let mut hashes = Map::new();
    if let Ok(bytes) = fs::read(path) {
        hashes.insert(
            "before_hash".to_owned(),
            Value::String(content_hash(&bytes)),
        );
    }
    hashes.insert(
        "after_hash".to_owned(),
        Value::String(content_hash(content.as_bytes())),
    );
    hashes
}
