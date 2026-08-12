//! Implements bounded textual and structural code search.

use std::fs;
use std::io::{self, Read};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, ExitStatus, Stdio};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

#[cfg(unix)]
use std::os::unix::process::{CommandExt, ExitStatusExt};

use serde_json::{json, Map, Value};

use crate::{AbortSignal, AgentError};

use super::{
    content_hash, error_result, integer_param, object, resolve_path, short_tag, string_param,
};

const MAX_TOOL_RESULT_CHARS: usize = 12_000;

/// Carries one completed subprocess result without exposing process handles.
///
/// # Examples
///
/// ```
/// let output = zeta_agent::CommandOutput {
///     status: 0,
///     stdout: "done".to_owned(),
///     stderr: String::new(),
///     timed_out: false,
///     duration_ms: 1,
/// };
/// assert_eq!(output.status, 0);
/// ```
pub struct CommandOutput {
    /// Carries the process exit status or `-1` when no code was available.
    pub status: i32,
    /// Carries standard output decoded with replacement characters.
    pub stdout: String,
    /// Carries standard error decoded with replacement characters.
    pub stderr: String,
    /// Reports whether the process group exceeded its deadline.
    pub timed_out: bool,
    /// Reports elapsed monotonic time in whole milliseconds.
    pub duration_ms: u64,
}

/// Runs subprocess-backed native operations behind one testable boundary.
///
/// # Examples
///
/// ```no_run
/// use std::path::Path;
/// use zeta_agent::{AbortReason, AbortSignal, CommandRunner, SystemCommandRunner};
///
/// struct Active;
/// impl AbortSignal for Active {
///     fn reason(&self) -> Option<AbortReason> { None }
/// }
///
/// let mut runner = SystemCommandRunner;
/// let output = runner.run("printf", &["ok".to_owned()], Path::new("."), &Active)?;
/// assert_eq!(output.status, 0);
/// # Ok::<(), std::io::Error>(())
/// ```
pub trait CommandRunner {
    /// Runs one program to completion in the invocation directory.
    ///
    /// # Errors
    ///
    /// Returns [`io::Error`] when the process cannot start or finish.
    fn run(
        &mut self,
        program: &str,
        arguments: &[String],
        base_directory: &Path,
        abort: &dyn AbortSignal,
    ) -> io::Result<CommandOutput>;

    /// Runs one shell command with a process-group deadline.
    ///
    /// # Examples
    ///
    /// ```no_run
    /// use std::path::Path;
    /// use std::time::Duration;
    /// use zeta_agent::{AbortReason, AbortSignal, CommandRunner, SystemCommandRunner};
    ///
    /// struct Active;
    /// impl AbortSignal for Active {
    ///     fn reason(&self) -> Option<AbortReason> { None }
    /// }
    ///
    /// let mut runner = SystemCommandRunner;
    /// let output = runner.run_shell(
    ///     "printf ok",
    ///     Path::new("."),
    ///     Duration::from_secs(1),
    ///     &Active,
    /// )?;
    /// assert!(!output.timed_out);
    /// # Ok::<(), std::io::Error>(())
    /// ```
    ///
    /// # Errors
    ///
    /// Returns [`io::Error`] when the shell cannot start, the process cannot be
    /// observed or terminated, or captured output cannot be read.
    fn run_shell(
        &mut self,
        command: &str,
        base_directory: &Path,
        timeout: Duration,
        abort: &dyn AbortSignal,
    ) -> io::Result<CommandOutput>;
}

/// Runs native-tool subprocesses through [`std::process::Command`].
///
/// # Examples
///
/// ```
/// let runner = zeta_agent::SystemCommandRunner;
/// let _ = runner;
/// ```
pub struct SystemCommandRunner;

impl CommandRunner for SystemCommandRunner {
    fn run(
        &mut self,
        program: &str,
        arguments: &[String],
        base_directory: &Path,
        abort: &dyn AbortSignal,
    ) -> io::Result<CommandOutput> {
        let mut command = Command::new(program);
        command.args(arguments).current_dir(base_directory);
        run_command(command, None, abort)
    }

    fn run_shell(
        &mut self,
        command: &str,
        base_directory: &Path,
        timeout: Duration,
        abort: &dyn AbortSignal,
    ) -> io::Result<CommandOutput> {
        run_shell_command(command, base_directory, timeout, abort)
    }
}

fn run_shell_command(
    source: &str,
    base_directory: &Path,
    timeout: Duration,
    abort: &dyn AbortSignal,
) -> io::Result<CommandOutput> {
    let mut command = Command::new("/bin/sh");
    command.arg("-c").arg(source).current_dir(base_directory);
    run_command(command, Some(timeout), abort)
}

fn run_command(
    mut command: Command,
    timeout: Option<Duration>,
    abort: &dyn AbortSignal,
) -> io::Result<CommandOutput> {
    if let Some(reason) = abort.reason() {
        return Err(interrupted(reason));
    }
    command.stdout(Stdio::piped()).stderr(Stdio::piped());
    #[cfg(unix)]
    command.process_group(0);
    let started = Instant::now();
    let mut child = command.spawn()?;
    let stdout = child
        .stdout
        .take()
        .ok_or_else(|| io::Error::other("shell stdout was not captured"))?;
    let stderr = child
        .stderr
        .take()
        .ok_or_else(|| io::Error::other("shell stderr was not captured"))?;
    let stdout = read_output(stdout);
    let stderr = read_output(stderr);
    let stop = wait_for_process(&mut child, &stdout, &stderr, started, timeout, abort)?;
    let stdout = join_output(stdout)?;
    let stderr = join_output(stderr)?;
    let (status, timed_out) = match stop {
        ProcessStop::Completed(status) => (status, false),
        ProcessStop::TimedOut(status) => (status, true),
        ProcessStop::Aborted(reason) => return Err(interrupted(reason)),
    };
    Ok(CommandOutput {
        status: exit_status(status),
        stdout: String::from_utf8_lossy(&stdout).into_owned(),
        stderr: String::from_utf8_lossy(&stderr).into_owned(),
        timed_out,
        duration_ms: elapsed_millis(started),
    })
}

enum ProcessStop {
    Completed(ExitStatus),
    TimedOut(ExitStatus),
    Aborted(crate::AbortReason),
}

fn read_output<R: Read + Send + 'static>(mut reader: R) -> JoinHandle<io::Result<Vec<u8>>> {
    thread::spawn(move || {
        let mut output = Vec::new();
        reader.read_to_end(&mut output)?;
        Ok(output)
    })
}

fn wait_for_process(
    child: &mut Child,
    stdout: &JoinHandle<io::Result<Vec<u8>>>,
    stderr: &JoinHandle<io::Result<Vec<u8>>>,
    started: Instant,
    timeout: Option<Duration>,
    abort: &dyn AbortSignal,
) -> io::Result<ProcessStop> {
    let mut status = None;
    loop {
        if status.is_none() {
            status = child.try_wait()?;
        }
        if stdout.is_finished() && stderr.is_finished() {
            let Some(status) = status else {
                continue;
            };
            return Ok(ProcessStop::Completed(status));
        }
        if let Some(reason) = abort.reason() {
            kill_process_group(child)?;
            if status.is_none() {
                let _status = child.wait()?;
            }
            return Ok(ProcessStop::Aborted(reason));
        }
        let elapsed = started.elapsed();
        if timeout.is_some_and(|timeout| elapsed >= timeout) {
            kill_process_group(child)?;
            let status = match status {
                Some(status) => status,
                None => child.wait()?,
            };
            return Ok(ProcessStop::TimedOut(status));
        }
        let sleep = timeout
            .map(|timeout| timeout.saturating_sub(elapsed))
            .unwrap_or(Duration::from_millis(10));
        thread::sleep(sleep.min(Duration::from_millis(10)));
    }
}

fn interrupted(reason: crate::AbortReason) -> io::Error {
    io::Error::new(
        io::ErrorKind::Interrupted,
        format!("tool execution aborted: {reason}"),
    )
}

fn join_output(output: JoinHandle<io::Result<Vec<u8>>>) -> io::Result<Vec<u8>> {
    output
        .join()
        .map_err(|_panic| io::Error::other("shell output reader panicked"))?
}

#[cfg(unix)]
fn kill_process_group(child: &mut Child) -> io::Result<()> {
    const SIGKILL: i32 = 9;

    unsafe extern "C" {
        fn kill(process: i32, signal: i32) -> i32;
    }

    let process_group = i32::try_from(child.id())
        .map_err(|_error| io::Error::other("child process id exceeds i32"))?;
    // SAFETY: `kill` accepts any i32 process selector; a negative child id
    // addresses the process group created for this child before it executed.
    let result = unsafe { kill(-process_group, SIGKILL) };
    if result == 0 {
        return Ok(());
    }
    let group_error = io::Error::last_os_error();
    match child.kill() {
        Ok(()) => Ok(()),
        Err(_child_error) => Err(group_error),
    }
}

#[cfg(not(unix))]
fn kill_process_group(child: &mut Child) -> io::Result<()> {
    child.kill()
}

#[cfg(unix)]
fn exit_status(status: ExitStatus) -> i32 {
    if let Some(code) = status.code() {
        return code;
    }
    let Some(signal) = status.signal() else {
        return -1;
    };
    -signal
}

#[cfg(not(unix))]
fn exit_status(status: ExitStatus) -> i32 {
    status.code().unwrap_or(-1)
}

fn elapsed_millis(started: Instant) -> u64 {
    u64::try_from(started.elapsed().as_millis()).unwrap_or(u64::MAX)
}

pub(super) fn grep<C: CommandRunner>(
    params: &Map<String, Value>,
    base_directory: &Path,
    commands: &mut C,
    abort: &dyn AbortSignal,
) -> Result<Map<String, Value>, AgentError> {
    let pattern = string_param(params, "pattern");
    if pattern.is_empty() {
        return Ok(error_result("missing-pattern", "missing pattern"));
    }
    let path_value = params.get("path").and_then(Value::as_str).unwrap_or(".");
    let path = resolve_path(path_value, base_directory);
    let limit = integer_param(params, "limit", 100);
    let arguments = vec![
        "--line-number".to_owned(),
        "--with-filename".to_owned(),
        "--color".to_owned(),
        "never".to_owned(),
        "--sort".to_owned(),
        "path".to_owned(),
        pattern.clone(),
        path.display().to_string(),
    ];
    let result = match commands.run("rg", &arguments, base_directory, abort) {
        Ok(output) => grep_command_result(output, limit),
        Err(error) if error.kind() == io::ErrorKind::NotFound => {
            match grep_fallback(&pattern, &path, limit, abort) {
                Ok(result) => result,
                Err(error) if error.kind() == io::ErrorKind::Interrupted => {
                    return Err(AgentError::tool(error.to_string()))
                }
                Err(error) => SearchResult::failure(error.to_string(), -1),
            }
        }
        Err(error) if error.kind() == io::ErrorKind::Interrupted => {
            return Err(AgentError::tool(error.to_string()))
        }
        Err(error) => SearchResult::failure(error.to_string(), -1),
    };
    Ok(render_grep_result(&pattern, &path, limit, result))
}

pub(super) fn ast_grep<C: CommandRunner>(
    params: &Map<String, Value>,
    base_directory: &Path,
    commands: &mut C,
    abort: &dyn AbortSignal,
) -> Result<Map<String, Value>, AgentError> {
    let pattern = string_param(params, "pattern");
    if pattern.is_empty() {
        return Ok(error_result("missing-pattern", "missing pattern"));
    }
    let language = string_param(params, "lang");
    if language.is_empty() {
        return Ok(error_result("missing-lang", "missing lang"));
    }
    let path_value = params.get("path").and_then(Value::as_str).unwrap_or(".");
    let path = resolve_path(path_value, base_directory);
    let limit = integer_param(params, "limit", 100);
    let arguments = vec![
        "run".to_owned(),
        "--pattern".to_owned(),
        pattern.clone(),
        "--lang".to_owned(),
        language.clone(),
        "--json=stream".to_owned(),
        path.display().to_string(),
    ];
    let output = match commands.run("sg", &arguments, base_directory, abort) {
        Ok(output) => output,
        Err(error) if error.kind() == io::ErrorKind::NotFound => {
            return Ok(error_result(
                "ast-grep-missing",
                "ast-grep executable 'sg' was not found",
            ))
        }
        Err(error) if error.kind() == io::ErrorKind::Interrupted => {
            return Err(AgentError::tool(error.to_string()))
        }
        Err(error) => return Ok(error_result("ast-grep-failed", error.to_string())),
    };
    let result = ast_command_result(output, limit);
    Ok(render_ast_result(&pattern, &language, &path, limit, result))
}

struct SearchResult {
    text: String,
    matches: usize,
    files: usize,
    truncated: bool,
    ok: bool,
    status: i32,
}

impl SearchResult {
    fn failure(text: String, status: i32) -> Self {
        SearchResult {
            text,
            matches: 0,
            files: 0,
            truncated: false,
            ok: false,
            status,
        }
    }
}

struct TextMatch {
    path: String,
    line_number: usize,
    text: String,
}

struct AstMatch {
    path: String,
    start_line: usize,
    lines: Vec<String>,
}

fn grep_command_result(output: CommandOutput, limit: usize) -> SearchResult {
    if output.status != 0 && output.status != 1 {
        return SearchResult::failure(output.stderr.trim().to_owned(), output.status);
    }
    let (lines, truncated) = limited_lines(&output.stdout, limit);
    search_result_from_lines(lines, truncated, output.status)
}

fn grep_fallback(
    pattern: &str,
    root: &Path,
    limit: usize,
    abort: &dyn AbortSignal,
) -> Result<SearchResult, io::Error> {
    let paths = fallback_paths(root, abort)?;
    let mut lines = Vec::new();
    let mut truncated = false;
    for path in paths {
        if let Some(reason) = abort.reason() {
            return Err(interrupted(reason));
        }
        let bytes = match fs::read(&path) {
            Ok(bytes) => bytes,
            Err(_error) => continue,
        };
        let text = String::from_utf8_lossy(&bytes);
        for (index, line) in text.lines().enumerate() {
            if !line.contains(pattern) {
                continue;
            }
            if lines.len() >= limit {
                truncated = true;
                break;
            }
            lines.push(format!("{}:{}:{line}", path.display(), index + 1));
        }
        if truncated {
            break;
        }
    }
    Ok(search_result_from_lines(lines, truncated, 0))
}

fn fallback_paths(root: &Path, abort: &dyn AbortSignal) -> Result<Vec<PathBuf>, io::Error> {
    if root.is_file() {
        return Ok(vec![root.to_owned()]);
    }
    let mut paths = Vec::new();
    collect_files(root, &mut paths, abort)?;
    Ok(paths)
}

fn collect_files(
    directory: &Path,
    paths: &mut Vec<PathBuf>,
    abort: &dyn AbortSignal,
) -> Result<(), io::Error> {
    if let Some(reason) = abort.reason() {
        return Err(interrupted(reason));
    }
    let mut entries = Vec::new();
    for entry in fs::read_dir(directory)? {
        entries.push(entry?.path());
    }
    entries.sort();
    for entry in entries {
        if entry.is_dir() {
            collect_files(&entry, paths, abort)?;
        } else {
            paths.push(entry);
        }
    }
    Ok(())
}

fn search_result_from_lines(lines: Vec<String>, truncated: bool, status: i32) -> SearchResult {
    let mut files = Vec::new();
    for line in &lines {
        let Some(found) = parse_text_match(line) else {
            continue;
        };
        if !files.contains(&found.path) {
            files.push(found.path);
        }
    }
    SearchResult {
        text: lines.join("\n"),
        matches: lines.len(),
        files: files.len(),
        truncated,
        ok: true,
        status,
    }
}

fn ast_command_result(output: CommandOutput, limit: usize) -> SearchResult {
    let (lines, truncated) = limited_lines(&output.stdout, limit);
    if output.status != 0 && output.status != 1 && !truncated {
        return SearchResult::failure(output.stderr.trim().to_owned(), output.status);
    }
    let mut files = Vec::new();
    for line in &lines {
        let Some(found) = parse_ast_match(line) else {
            continue;
        };
        if !files.contains(&found.path) {
            files.push(found.path);
        }
    }
    SearchResult {
        text: lines.join("\n"),
        matches: lines.len(),
        files: files.len(),
        truncated,
        ok: true,
        status: output.status,
    }
}

fn limited_lines(text: &str, limit: usize) -> (Vec<String>, bool) {
    let mut lines = Vec::new();
    let mut truncated = false;
    for line in text.lines() {
        if lines.len() >= limit {
            truncated = true;
            break;
        }
        lines.push(line.to_owned());
    }
    (lines, truncated)
}

fn render_grep_result(
    pattern: &str,
    path: &Path,
    limit: usize,
    result: SearchResult,
) -> Map<String, Value> {
    let (text, tags) = tagged_text(&result);
    result_value(pattern, None, path, limit, result, text, tags)
}

fn render_ast_result(
    pattern: &str,
    language: &str,
    path: &Path,
    limit: usize,
    result: SearchResult,
) -> Map<String, Value> {
    let (text, tags) = ast_tagged_text(&result);
    result_value(pattern, Some(language), path, limit, result, text, tags)
}

fn result_value(
    pattern: &str,
    language: Option<&str>,
    path: &Path,
    limit: usize,
    result: SearchResult,
    text: String,
    tags: Map<String, Value>,
) -> Map<String, Value> {
    let (text, content_truncated) = truncate_chars(&text, MAX_TOOL_RESULT_CHARS);
    let mut metadata = object(json!({
        "pattern": pattern,
        "path": path.display().to_string(),
        "limit": limit,
        "matches": result.matches,
        "files": result.files,
        "truncated": result.truncated || content_truncated,
        "match_limit_reached": result.truncated,
        "content_truncated": content_truncated,
        "max_chars": MAX_TOOL_RESULT_CHARS,
        "status": result.status,
        "tags": tags,
    }));
    if let Some(language) = language {
        metadata.insert("lang".to_owned(), Value::String(language.to_owned()));
    }
    object(json!({
        "ok": result.ok,
        "content": [{"type": "text", "text": text}],
        "metadata": metadata,
    }))
}

fn tagged_text(result: &SearchResult) -> (String, Map<String, Value>) {
    if !result.ok {
        return (result.text.clone(), Map::new());
    }
    let mut rendered = Vec::new();
    let mut tags = Map::new();
    let mut current_path = String::new();
    for line in result.text.lines() {
        let Some(found) = parse_text_match(line) else {
            continue;
        };
        if found.path != current_path {
            let Some(tag) = tag_for_path(&found.path) else {
                continue;
            };
            tags.insert(found.path.clone(), Value::String(tag.clone()));
            rendered.push(format!("[{}#{tag}]", found.path));
            current_path = found.path.clone();
        }
        rendered.push(format!("{}:{}", found.line_number, found.text));
    }
    if rendered.is_empty() {
        return (result.text.clone(), tags);
    }
    (rendered.join("\n"), tags)
}

fn ast_tagged_text(result: &SearchResult) -> (String, Map<String, Value>) {
    if !result.ok {
        return (result.text.clone(), Map::new());
    }
    let mut rendered = Vec::new();
    let mut tags = Map::new();
    let mut current_path = String::new();
    for line in result.text.lines() {
        let Some(found) = parse_ast_match(line) else {
            continue;
        };
        if found.path != current_path {
            let Some(tag) = tag_for_path(&found.path) else {
                continue;
            };
            tags.insert(found.path.clone(), Value::String(tag.clone()));
            rendered.push(format!("[{}#{tag}]", found.path));
            current_path = found.path.clone();
        }
        for (offset, line) in found.lines.iter().enumerate() {
            rendered.push(format!("{}:{line}", found.start_line + offset));
        }
    }
    if rendered.is_empty() {
        return (result.text.clone(), tags);
    }
    (rendered.join("\n"), tags)
}

fn parse_text_match(line: &str) -> Option<TextMatch> {
    let (path, rest) = line.split_once(':')?;
    let (line_number, text) = rest.split_once(':')?;
    let line_number = line_number.parse().ok()?;
    Some(TextMatch {
        path: path.to_owned(),
        line_number,
        text: text.to_owned(),
    })
}

fn parse_ast_match(line: &str) -> Option<AstMatch> {
    let value: Value = serde_json::from_str(line).ok()?;
    let value = value.as_object()?;
    let path = value.get("file")?.as_str()?;
    let lines = value.get("lines")?.as_str()?;
    let range = value.get("range")?.as_object()?;
    let start = range.get("start")?.as_object()?;
    let start_line = usize::try_from(start.get("line")?.as_u64()?).ok()? + 1;
    let mut parsed_lines = Vec::new();
    for line in lines.lines() {
        parsed_lines.push(line.to_owned());
    }
    Some(AstMatch {
        path: path.to_owned(),
        start_line,
        lines: parsed_lines,
    })
}

fn tag_for_path(path: &str) -> Option<String> {
    let bytes = fs::read(path).ok()?;
    let hash = content_hash(&bytes);
    Some(short_tag(&hash).to_owned())
}

fn truncate_chars(text: &str, limit: usize) -> (String, bool) {
    if text.chars().count() <= limit {
        return (text.to_owned(), false);
    }
    let mut truncated = String::new();
    for character in text.chars().take(limit) {
        truncated.push(character);
    }
    (truncated, true)
}
