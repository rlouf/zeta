//! Loads a project and persists its immutable revisions.

use std::collections::BTreeMap;
use std::fmt;
use std::fs::{self, OpenOptions};
use std::io::Write;
use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::str::FromStr;
use std::sync::atomic::{AtomicU64, Ordering};

use serde::{Deserialize, Serialize};
use zeta_dispatch::{EventPattern, Route, SessionRule};
use zeta_manifest::{load_agent, parse_agent, AgentSpec};
use zeta_substrate::{canonical_json, hash_bytes};

use crate::{PythonProviderCatalog, PythonProviderHost};

const ACTIVE_PROJECT_SCHEMA: &str = "zeta.active_project";
const ACTIVE_PROJECT_VERSION: u64 = 3;

static NEXT_TEMPORARY_ID: AtomicU64 = AtomicU64::new(0);

/// Reports a project-source or revision failure.
#[derive(Debug)]
pub struct ProjectError {
    detail: String,
}

impl ProjectError {
    fn new(detail: impl Into<String>) -> Self {
        Self {
            detail: detail.into(),
        }
    }
}

impl fmt::Display for ProjectError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.detail)
    }
}

impl std::error::Error for ProjectError {}

/// Locates the editable source directory for one project.
#[derive(Clone, Debug)]
pub struct Project {
    root: PathBuf,
}

impl Project {
    /// Opens one existing project source directory.
    pub fn open(root: impl AsRef<Path>) -> Result<Self, ProjectError> {
        let root = root.as_ref();
        let metadata = fs::symlink_metadata(root).map_err(|error| {
            ProjectError::new(format!(
                "cannot inspect project '{}': {error}",
                root.display()
            ))
        })?;
        if metadata.file_type().is_symlink() || !metadata.is_dir() {
            return Err(ProjectError::new(format!(
                "project is not a directory: {}",
                root.display()
            )));
        }
        Ok(Self {
            root: root.to_path_buf(),
        })
    }

    /// Returns the editable project directory.
    pub fn root(&self) -> &Path {
        &self.root
    }

    /// Loads the current immutable revision from project source.
    pub fn revision(&self) -> Result<ProjectRevision, ProjectError> {
        ProjectRevision::load(&self.root)
    }
}

/// Describes one enabled agent in the active project revision.
#[derive(Clone, Debug, Deserialize, PartialEq, Eq, Serialize)]
pub struct ActiveAgent {
    /// Identifies the agent file and stable runtime identity.
    pub slug: String,
    /// Contains the authored display name.
    pub name: String,
    /// Contains the authored summary.
    pub description: String,
    /// Counts the agent's authored recurring schedules.
    pub schedule_count: usize,
    /// Identifies the exact loaded Markdown source.
    pub source_address: String,
}

/// Returns a safe public view of the active project revision.
#[derive(Clone, Debug, Deserialize, PartialEq, Eq, Serialize)]
pub struct ActiveProjectStatus {
    /// Identifies the immutable active revision.
    pub revision_id: String,
    /// Lists enabled agents in slug order.
    pub agents: Vec<ActiveAgent>,
}

/// Stores immutable project revisions for active and queued work.
#[derive(Clone, Debug)]
pub struct ProjectRevisionStore {
    directory: PathBuf,
}

impl ProjectRevisionStore {
    /// Creates a store rooted at one private runtime directory.
    pub fn new(directory: impl Into<PathBuf>) -> Self {
        Self {
            directory: directory.into(),
        }
    }

    /// Persists one verified revision under its content identity.
    pub fn record(&self, revision: &ProjectRevision) -> Result<(), ProjectError> {
        ensure_archive_directory(&self.directory)?;
        revision.write(&self.path_for(revision.revision_id()))
    }

    /// Loads the immutable revision identified by one queued item.
    pub fn load(&self, revision_id: &str) -> Result<Option<ProjectRevision>, ProjectError> {
        let path = self.path_for(revision_id);
        let revision = ProjectRevision::read(&path)?;
        if let Some(revision) = &revision {
            if revision.revision_id() != revision_id {
                return Err(ProjectError::new(format!(
                    "revision store has a different revision at '{}': {revision_id}",
                    path.display()
                )));
            }
        }
        Ok(revision)
    }

    fn path_for(&self, revision_id: &str) -> PathBuf {
        self.directory
            .join(format!("{}.json", hash_bytes(revision_id.as_bytes())))
    }
}

/// Stores one immutable project revision that a runtime can activate.
///
/// The source revision retains full agent declarations. Later
/// runtime stages can construct execution manifests without rereading draft
/// project files.
#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ProjectRevision {
    schema: String,
    version: u64,
    project_root: PathBuf,
    revision_id: String,
    agents: BTreeMap<String, AgentSpec>,
    providers: PythonProviderCatalog,
}

impl ProjectRevision {
    /// Loads direct Markdown agent declarations from one project root.
    pub(crate) fn load(project_root: &Path) -> Result<Self, ProjectError> {
        let agents_directory = project_root.join("agents");
        let directory_metadata = fs::symlink_metadata(&agents_directory).map_err(|error| {
            ProjectError::new(format!(
                "cannot inspect agents directory '{}': {error}",
                agents_directory.display()
            ))
        })?;
        if directory_metadata.file_type().is_symlink() || !directory_metadata.is_dir() {
            return Err(ProjectError::new(format!(
                "agents path is not a directory: {}",
                agents_directory.display()
            )));
        }

        let mut paths = Vec::new();
        let entries = fs::read_dir(&agents_directory).map_err(|error| {
            ProjectError::new(format!(
                "cannot read agents directory '{}': {error}",
                agents_directory.display()
            ))
        })?;
        for entry in entries {
            let entry = entry.map_err(|error| {
                ProjectError::new(format!(
                    "cannot read an entry in '{}': {error}",
                    agents_directory.display()
                ))
            })?;
            let path = entry.path();
            let file_type = entry.file_type().map_err(|error| {
                ProjectError::new(format!(
                    "cannot inspect agent entry '{}': {error}",
                    path.display()
                ))
            })?;
            if file_type.is_symlink() {
                return Err(ProjectError::new(format!(
                    "agent entry is a symbolic link: {}",
                    path.display()
                )));
            }
            if file_type.is_file() && path.extension().is_some_and(|extension| extension == "md") {
                paths.push(path);
            }
        }
        paths.sort();
        if paths.is_empty() {
            return Err(ProjectError::new(format!(
                "agents directory contains no Markdown agents: {}",
                agents_directory.display()
            )));
        }

        let mut agents = BTreeMap::new();
        for path in paths {
            let agent = load_agent(&path).map_err(|error| {
                ProjectError::new(format!("cannot load agent '{}': {error}", path.display()))
            })?;
            let slug = agent.slug.clone();
            if agents.insert(slug.clone(), agent).is_some() {
                return Err(ProjectError::new(format!(
                    "agents directory defines duplicate slug {slug:?}"
                )));
            }
        }
        if !agents.values().any(|agent| agent.enabled) {
            return Err(ProjectError::new(format!(
                "agents directory has no enabled agents: {}",
                agents_directory.display()
            )));
        }
        Self::from_agents(project_root, agents)
    }

    /// Reads and verifies the active project document when it exists.
    pub fn read(path: &Path) -> Result<Option<Self>, ProjectError> {
        let bytes = match fs::read(path) {
            Ok(bytes) => bytes,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
            Err(error) => {
                return Err(ProjectError::new(format!(
                    "cannot read active project '{}': {error}",
                    path.display()
                )))
            }
        };
        let revision: ProjectRevision = serde_json::from_slice(&bytes).map_err(|error| {
            ProjectError::new(format!(
                "active project '{}' is invalid JSON: {error}",
                path.display()
            ))
        })?;
        revision.validate(path)?;
        Ok(Some(revision))
    }

    /// Atomically replaces the active project document.
    pub fn write(&self, path: &Path) -> Result<(), ProjectError> {
        self.validate(path)?;
        let mut bytes = serde_json::to_vec(self).map_err(|error| {
            ProjectError::new(format!(
                "cannot encode active project '{}': {error}",
                path.display()
            ))
        })?;
        bytes.push(b'\n');
        write_atomic(path, &bytes)
    }

    /// Returns the immutable revision identity.
    pub fn revision_id(&self) -> &str {
        &self.revision_id
    }

    /// Returns the project root that produced this revision.
    pub fn project_root(&self) -> &Path {
        &self.project_root
    }

    /// Returns enabled agents in stable slug order.
    pub fn active_agents(&self) -> Vec<ActiveAgent> {
        self.agents
            .values()
            .filter(|agent| agent.enabled)
            .map(|agent| ActiveAgent {
                slug: agent.slug.clone(),
                name: agent.name.clone(),
                description: agent.description.clone(),
                schedule_count: agent.schedules.len(),
                source_address: agent.content_address.to_string(),
            })
            .collect()
    }

    /// Returns an exact enabled or disabled agent declaration by slug.
    pub fn agent(&self, slug: &str) -> Option<&AgentSpec> {
        self.agents.get(slug)
    }

    /// Returns all loaded agent declarations in stable slug order.
    pub(crate) fn agents(&self) -> impl Iterator<Item = &AgentSpec> {
        self.agents.values()
    }

    /// Returns the Python providers selected for this revision.
    pub fn providers(&self) -> &PythonProviderCatalog {
        &self.providers
    }

    /// Returns the public active-revision view.
    pub fn status(&self) -> ActiveProjectStatus {
        ActiveProjectStatus {
            revision_id: self.revision_id().to_owned(),
            agents: self.active_agents(),
        }
    }

    /// Compiles enabled agent declarations into durable ingress routes.
    pub fn routes(&self) -> Result<Vec<Route>, ProjectError> {
        let mut routes = Vec::new();
        for agent in self.agents.values() {
            if !agent.enabled {
                continue;
            }
            let session = SessionRule::from_str(&agent.session).map_err(|error| {
                ProjectError::new(format!(
                    "agent {:?} has an invalid session rule: {error}",
                    agent.slug
                ))
            })?;
            routes.push(Route::new(
                agent.slug.clone(),
                agent
                    .accepts
                    .iter()
                    .cloned()
                    .map(EventPattern::new)
                    .collect(),
                session,
                agent.locks.clone(),
                Some(self.revision_id.clone()),
            ));
        }
        Ok(routes)
    }

    fn from_agents(
        project_root: &Path,
        agents: BTreeMap<String, AgentSpec>,
    ) -> Result<Self, ProjectError> {
        let project_root = fs::canonicalize(project_root).map_err(|error| {
            ProjectError::new(format!(
                "cannot resolve project root '{}': {error}",
                project_root.display()
            ))
        })?;
        let providers = load_python_providers(&project_root)?;
        let mut revision = Self {
            schema: ACTIVE_PROJECT_SCHEMA.to_owned(),
            version: ACTIVE_PROJECT_VERSION,
            project_root,
            revision_id: String::new(),
            agents,
            providers,
        };
        revision.validate_provider_references(&revision.project_root)?;
        revision.revision_id = revision.expected_id()?;
        Ok(revision)
    }

    fn validate(&self, path: &Path) -> Result<(), ProjectError> {
        if self.schema != ACTIVE_PROJECT_SCHEMA || self.version != ACTIVE_PROJECT_VERSION {
            return Err(ProjectError::new(format!(
                "active project '{}' has unsupported schema or version",
                path.display()
            )));
        }
        if !self.project_root.is_absolute() {
            return Err(ProjectError::new(format!(
                "active project '{}' has a relative project root",
                path.display()
            )));
        }
        if self.agents.is_empty() {
            return Err(ProjectError::new(format!(
                "active project '{}' has no agents",
                path.display()
            )));
        }
        if !self.agents.values().any(|agent| agent.enabled) {
            return Err(ProjectError::new(format!(
                "active project '{}' has no enabled agents",
                path.display()
            )));
        }
        for (slug, agent) in &self.agents {
            if agent.slug != *slug {
                return Err(ProjectError::new(format!(
                    "active project '{}' stores agent {slug:?} under a different slug",
                    path.display()
                )));
            }
            let parsed = parse_agent(slug, agent.source.as_bytes()).map_err(|error| {
                ProjectError::new(format!(
                    "active project '{}' has invalid source for {slug:?}: {error}",
                    path.display()
                ))
            })?;
            if parsed != *agent {
                return Err(ProjectError::new(format!(
                    "active project '{}' has altered source metadata for {slug:?}",
                    path.display()
                )));
            }
        }
        self.validate_provider_references(path)?;
        let expected = self.expected_id()?;
        if self.revision_id != expected {
            return Err(ProjectError::new(format!(
                "active project '{}' has an invalid revision id",
                path.display()
            )));
        }
        Ok(())
    }

    fn expected_id(&self) -> Result<String, ProjectError> {
        let value = serde_json::json!({
            "schema": self.schema,
            "version": self.version,
            "project_root": self.project_root,
            "agents": self.agents,
            "providers": self.providers,
        });
        let bytes = canonical_json(&value).map_err(|error| {
            ProjectError::new(format!("cannot calculate project revision id: {error}"))
        })?;
        Ok(format!("project:{}", hash_bytes(&bytes)))
    }

    fn validate_provider_references(&self, path: &Path) -> Result<(), ProjectError> {
        let native = zeta_agent::native_capabilities();
        for agent in self.agents.values().filter(|agent| agent.enabled) {
            if agent.tools_inherit {
                continue;
            }
            for tool in &agent.tools {
                let selected_python = self.providers.tools().contains_key(tool);
                let selected_native = native.iter().any(|capability| {
                    capability.id.as_str() == tool || capability.id.model_name() == tool
                });
                if !selected_python && !selected_native {
                    return Err(ProjectError::new(format!(
                        "active project '{}' has agent {:?} with unavailable tool {tool:?}",
                        path.display(),
                        agent.slug
                    )));
                }
            }
        }
        Ok(())
    }
}

fn load_python_providers(project_root: &Path) -> Result<PythonProviderCatalog, ProjectError> {
    if !has_python_provider_scope(project_root) {
        return Ok(PythonProviderCatalog::default());
    }
    let host = PythonProviderHost::start(project_root).map_err(|error| {
        ProjectError::new(format!(
            "cannot load Python providers for '{}': {error}",
            project_root.display()
        ))
    })?;
    Ok(host.catalog().clone())
}

fn has_python_provider_scope(project_root: &Path) -> bool {
    ["models", "tools", "connectors"]
        .iter()
        .any(|directory| project_root.join(directory).is_dir())
        || project_root.join(".venv").is_dir()
}

fn write_atomic(path: &Path, bytes: &[u8]) -> Result<(), ProjectError> {
    let parent = path.parent().ok_or_else(|| {
        ProjectError::new(format!(
            "active project path has no parent: {}",
            path.display()
        ))
    })?;
    let parent_metadata = fs::metadata(parent).map_err(|error| {
        ProjectError::new(format!(
            "cannot inspect active project directory '{}': {error}",
            parent.display()
        ))
    })?;
    if !parent_metadata.is_dir() {
        return Err(ProjectError::new(format!(
            "active project parent is not a directory: {}",
            parent.display()
        )));
    }
    match fs::symlink_metadata(path) {
        Ok(metadata) if metadata.file_type().is_symlink() || !metadata.is_file() => {
            return Err(ProjectError::new(format!(
                "active project path is not a regular file: {}",
                path.display()
            )))
        }
        Ok(_metadata) => {}
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
        Err(error) => {
            return Err(ProjectError::new(format!(
                "cannot inspect active project '{}': {error}",
                path.display()
            )))
        }
    }

    let name = path
        .file_name()
        .and_then(|name| name.to_str())
        .ok_or_else(|| {
            ProjectError::new(format!(
                "active project path has no UTF-8 name: {}",
                path.display()
            ))
        })?;
    let temporary = loop {
        let id = NEXT_TEMPORARY_ID.fetch_add(1, Ordering::Relaxed);
        let candidate = parent.join(format!(".{name}.{}.{}.tmp", std::process::id(), id));
        let opened = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(&candidate);
        match opened {
            Ok(file) => break (candidate, file),
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => continue,
            Err(error) => {
                return Err(ProjectError::new(format!(
                    "cannot create active project temporary file in '{}': {error}",
                    parent.display()
                )))
            }
        }
    };
    let (temporary_path, mut temporary_file) = temporary;
    let result = temporary_file
        .write_all(bytes)
        .and_then(|()| temporary_file.sync_all())
        .map_err(|error| {
            ProjectError::new(format!(
                "cannot write active project temporary file '{}': {error}",
                temporary_path.display()
            ))
        });
    drop(temporary_file);
    if let Err(error) = result {
        let _removed = fs::remove_file(&temporary_path);
        return Err(error);
    }
    fs::rename(&temporary_path, path).map_err(|error| {
        let _removed = fs::remove_file(&temporary_path);
        ProjectError::new(format!(
            "cannot activate project '{}': {error}",
            path.display()
        ))
    })?;
    Ok(())
}

fn ensure_archive_directory(directory: &Path) -> Result<(), ProjectError> {
    match fs::symlink_metadata(directory) {
        Ok(metadata) if metadata.file_type().is_symlink() || !metadata.is_dir() => {
            return Err(ProjectError::new(format!(
                "revision archive is not a directory: {}",
                directory.display()
            )));
        }
        Ok(_) => {}
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            fs::create_dir_all(directory).map_err(|error| {
                ProjectError::new(format!(
                    "cannot create revision archive '{}': {error}",
                    directory.display()
                ))
            })?;
        }
        Err(error) => {
            return Err(ProjectError::new(format!(
                "cannot inspect revision archive '{}': {error}",
                directory.display()
            )));
        }
    }
    let metadata = fs::symlink_metadata(directory).map_err(|error| {
        ProjectError::new(format!(
            "cannot inspect revision archive '{}': {error}",
            directory.display()
        ))
    })?;
    if metadata.file_type().is_symlink() || !metadata.is_dir() {
        return Err(ProjectError::new(format!(
            "revision archive is not a directory: {}",
            directory.display()
        )));
    }
    fs::set_permissions(directory, fs::Permissions::from_mode(0o700)).map_err(|error| {
        ProjectError::new(format!(
            "cannot secure revision archive '{}': {error}",
            directory.display()
        ))
    })?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use std::fs;

    use tempfile::TempDir;

    use super::*;

    fn write_agent(root: &Path, name: &str, enabled: bool) {
        let agents = root.join("agents");
        fs::create_dir_all(&agents).expect("agents directory");
        fs::write(
            agents.join(format!("{name}.md")),
            format!(
                "---\nname: {}\ndescription: Reports current state.\nenabled: {enabled}\n---\nReport current state.\n",
                name.replace('-', " ")
            ),
        )
        .expect("agent source");
    }

    #[test]
    fn load_builds_a_stable_revision_and_filters_disabled_agents() {
        let temporary = TempDir::new().expect("temporary directory");
        write_agent(temporary.path(), "zulu", true);
        write_agent(temporary.path(), "alpha", false);

        let revision = ProjectRevision::load(temporary.path()).expect("project loads");
        assert!(revision.revision_id().starts_with("project:b3:"));
        assert_eq!(
            revision.status().agents,
            vec![ActiveAgent {
                slug: "zulu".to_owned(),
                name: "zulu".to_owned(),
                description: "Reports current state.".to_owned(),
                schedule_count: 0,
                source_address: revision.agents["zulu"].content_address.to_string(),
            }]
        );
    }

    #[test]
    fn write_and_read_preserve_the_exact_revision() {
        let temporary = TempDir::new().expect("temporary directory");
        write_agent(temporary.path(), "worker", true);
        let revision = ProjectRevision::load(temporary.path()).expect("project loads");
        let state = temporary.path().join("state");
        fs::create_dir(&state).expect("state directory");
        let active = state.join("active-project.json");

        revision.write(&active).expect("project writes");
        let restored = ProjectRevision::read(&active)
            .expect("project reads")
            .expect("active project exists");
        assert_eq!(restored, revision);
    }

    #[test]
    fn load_rejects_symbolic_link_agent_entries() {
        use std::os::unix::fs::symlink;

        let temporary = TempDir::new().expect("temporary directory");
        write_agent(temporary.path(), "worker", true);
        let external = temporary.path().join("external.md");
        fs::write(
            &external,
            "---\nname: External\ndescription: External.\n---\nExternal.\n",
        )
        .expect("external agent");
        symlink(&external, temporary.path().join("agents/linked.md")).expect("agent link");

        let error = ProjectRevision::load(temporary.path()).expect_err("link must fail");
        assert!(error.to_string().contains("symbolic link"));
    }

    #[test]
    fn load_rejects_a_project_with_no_enabled_agents() {
        let temporary = TempDir::new().expect("temporary directory");
        write_agent(temporary.path(), "worker", false);

        let error = ProjectRevision::load(temporary.path()).expect_err("project must fail");
        assert!(error.to_string().contains("no enabled agents"));
    }

    #[test]
    fn load_rejects_an_unavailable_explicit_tool() {
        let temporary = TempDir::new().expect("temporary directory");
        let agents = temporary.path().join("agents");
        fs::create_dir(&agents).expect("agents directory");
        fs::write(
            agents.join("worker.md"),
            "---\nname: Worker\ndescription: Runs one tool.\ntools: [missing]\n---\nRun.\n",
        )
        .expect("agent source");

        let error = ProjectRevision::load(temporary.path()).expect_err("tool must fail");

        assert!(error.to_string().contains("unavailable tool \"missing\""));
    }

    #[test]
    fn routes_preserve_agent_session_locks_and_revision() {
        let temporary = TempDir::new().expect("temporary directory");
        let agents = temporary.path().join("agents");
        fs::create_dir(&agents).expect("agents directory");
        fs::write(
            agents.join("worker.md"),
            "---\nname: Worker\ndescription: Routes work.\nsession: shared\naccepts: [example.created]\nlocks: [account]\n---\nRoute work.\n",
        )
        .expect("agent source");
        let revision = ProjectRevision::load(temporary.path()).expect("project loads");
        let routes = revision.routes().expect("routes compile");
        let event = zeta_journal::Event {
            id: "event-1".to_owned(),
            event_type: "example.created".to_owned(),
            source: "test".to_owned(),
            payload: serde_json::Map::new(),
            idempotency_key: None,
            caused_by: None,
            session_id: None,
            run_id: None,
            turn_id: None,
            timestamp_ms: 1,
            cursor: None,
        };

        let decisions = zeta_dispatch::route_event(&event, &routes).expect("event routes");
        assert_eq!(decisions.len(), 1);
        assert_eq!(decisions[0].agent_id(), "worker");
        assert_eq!(decisions[0].session_id().as_str(), "agent/worker");
        assert_eq!(decisions[0].lock_keys(), ["account"]);
        assert_eq!(
            decisions[0].project_revision(),
            Some(revision.revision_id())
        );
    }
}
