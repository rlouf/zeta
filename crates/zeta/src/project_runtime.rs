//! Loads and persists one explicit native project generation.

use std::collections::BTreeMap;
use std::fmt;
use std::fs::{self, OpenOptions};
use std::io::Write;
use std::os::unix::fs::OpenOptionsExt;
use std::path::{Path, PathBuf};
use std::str::FromStr;
use std::sync::atomic::{AtomicU64, Ordering};

use serde::{Deserialize, Serialize};
use zeta_dispatch::{EventPattern, Route, SessionRule};
use zeta_manifest::{load_agent, parse_agent, AgentSpec};
use zeta_substrate::{canonical_json, hash_bytes};

const ACTIVE_PROJECT_SCHEMA: &str = "zeta.active_project";
const ACTIVE_PROJECT_VERSION: u64 = 1;

static NEXT_TEMPORARY_ID: AtomicU64 = AtomicU64::new(0);

/// Reports a project-source or active-generation failure.
#[derive(Debug)]
pub struct ProjectRuntimeError {
    detail: String,
}

impl ProjectRuntimeError {
    fn new(detail: impl Into<String>) -> Self {
        Self {
            detail: detail.into(),
        }
    }
}

impl fmt::Display for ProjectRuntimeError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.detail)
    }
}

impl std::error::Error for ProjectRuntimeError {}

/// Describes one enabled agent in the active project generation.
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

/// Returns a safe public view of the active project generation.
#[derive(Clone, Debug, Deserialize, PartialEq, Eq, Serialize)]
pub struct ActiveProjectStatus {
    /// Identifies the immutable active generation.
    pub generation_id: String,
    /// Lists enabled agents in slug order.
    pub agents: Vec<ActiveAgent>,
}

/// Stores one immutable generation that a native runtime can activate.
///
/// The source snapshot deliberately retains full agent declarations. Later
/// runtime stages can construct execution manifests without rereading draft
/// project files.
#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ProjectGeneration {
    schema: String,
    version: u64,
    project_root: PathBuf,
    generation_id: String,
    agents: BTreeMap<String, AgentSpec>,
}

impl ProjectGeneration {
    /// Loads direct Markdown agent declarations from one project root.
    pub fn load(project_root: &Path) -> Result<Self, ProjectRuntimeError> {
        let agents_directory = project_root.join("agents");
        let directory_metadata = fs::symlink_metadata(&agents_directory).map_err(|error| {
            ProjectRuntimeError::new(format!(
                "cannot inspect agents directory '{}': {error}",
                agents_directory.display()
            ))
        })?;
        if directory_metadata.file_type().is_symlink() || !directory_metadata.is_dir() {
            return Err(ProjectRuntimeError::new(format!(
                "agents path is not a directory: {}",
                agents_directory.display()
            )));
        }

        let mut paths = Vec::new();
        let entries = fs::read_dir(&agents_directory).map_err(|error| {
            ProjectRuntimeError::new(format!(
                "cannot read agents directory '{}': {error}",
                agents_directory.display()
            ))
        })?;
        for entry in entries {
            let entry = entry.map_err(|error| {
                ProjectRuntimeError::new(format!(
                    "cannot read an entry in '{}': {error}",
                    agents_directory.display()
                ))
            })?;
            let path = entry.path();
            let file_type = entry.file_type().map_err(|error| {
                ProjectRuntimeError::new(format!(
                    "cannot inspect agent entry '{}': {error}",
                    path.display()
                ))
            })?;
            if file_type.is_symlink() {
                return Err(ProjectRuntimeError::new(format!(
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
            return Err(ProjectRuntimeError::new(format!(
                "agents directory contains no Markdown agents: {}",
                agents_directory.display()
            )));
        }

        let mut agents = BTreeMap::new();
        for path in paths {
            let agent = load_agent(&path).map_err(|error| {
                ProjectRuntimeError::new(format!("cannot load agent '{}': {error}", path.display()))
            })?;
            let slug = agent.slug.clone();
            if agents.insert(slug.clone(), agent).is_some() {
                return Err(ProjectRuntimeError::new(format!(
                    "agents directory defines duplicate slug {slug:?}"
                )));
            }
        }
        Self::from_agents(project_root, agents)
    }

    /// Reads and verifies the active project document when it exists.
    pub fn read(path: &Path) -> Result<Option<Self>, ProjectRuntimeError> {
        let bytes = match fs::read(path) {
            Ok(bytes) => bytes,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
            Err(error) => {
                return Err(ProjectRuntimeError::new(format!(
                    "cannot read active project '{}': {error}",
                    path.display()
                )))
            }
        };
        let generation: ProjectGeneration = serde_json::from_slice(&bytes).map_err(|error| {
            ProjectRuntimeError::new(format!(
                "active project '{}' is invalid JSON: {error}",
                path.display()
            ))
        })?;
        generation.validate(path)?;
        Ok(Some(generation))
    }

    /// Atomically replaces the active project document.
    pub fn write(&self, path: &Path) -> Result<(), ProjectRuntimeError> {
        self.validate(path)?;
        let mut bytes = serde_json::to_vec(self).map_err(|error| {
            ProjectRuntimeError::new(format!(
                "cannot encode active project '{}': {error}",
                path.display()
            ))
        })?;
        bytes.push(b'\n');
        write_atomic(path, &bytes)
    }

    /// Returns the immutable active-generation identity.
    pub fn generation_id(&self) -> &str {
        &self.generation_id
    }

    /// Returns the project root that produced this generation.
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

    /// Returns the public active-generation view.
    pub fn status(&self) -> ActiveProjectStatus {
        ActiveProjectStatus {
            generation_id: self.generation_id.clone(),
            agents: self.active_agents(),
        }
    }

    /// Compiles enabled agent declarations into durable ingress routes.
    pub fn routes(&self) -> Result<Vec<Route>, ProjectRuntimeError> {
        let mut routes = Vec::new();
        for agent in self.agents.values() {
            if !agent.enabled {
                continue;
            }
            let session = SessionRule::from_str(&agent.session).map_err(|error| {
                ProjectRuntimeError::new(format!(
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
                Some(self.generation_id.clone()),
            ));
        }
        Ok(routes)
    }

    fn from_agents(
        project_root: &Path,
        agents: BTreeMap<String, AgentSpec>,
    ) -> Result<Self, ProjectRuntimeError> {
        let project_root = fs::canonicalize(project_root).map_err(|error| {
            ProjectRuntimeError::new(format!(
                "cannot resolve project root '{}': {error}",
                project_root.display()
            ))
        })?;
        let mut generation = Self {
            schema: ACTIVE_PROJECT_SCHEMA.to_owned(),
            version: ACTIVE_PROJECT_VERSION,
            project_root,
            generation_id: String::new(),
            agents,
        };
        generation.generation_id = generation.expected_id()?;
        Ok(generation)
    }

    fn validate(&self, path: &Path) -> Result<(), ProjectRuntimeError> {
        if self.schema != ACTIVE_PROJECT_SCHEMA || self.version != ACTIVE_PROJECT_VERSION {
            return Err(ProjectRuntimeError::new(format!(
                "active project '{}' has unsupported schema or version",
                path.display()
            )));
        }
        if !self.project_root.is_absolute() {
            return Err(ProjectRuntimeError::new(format!(
                "active project '{}' has a relative project root",
                path.display()
            )));
        }
        if self.agents.is_empty() {
            return Err(ProjectRuntimeError::new(format!(
                "active project '{}' has no agents",
                path.display()
            )));
        }
        for (slug, agent) in &self.agents {
            if agent.slug != *slug {
                return Err(ProjectRuntimeError::new(format!(
                    "active project '{}' stores agent {slug:?} under a different slug",
                    path.display()
                )));
            }
            let parsed = parse_agent(slug, agent.source.as_bytes()).map_err(|error| {
                ProjectRuntimeError::new(format!(
                    "active project '{}' has invalid source for {slug:?}: {error}",
                    path.display()
                ))
            })?;
            if parsed != *agent {
                return Err(ProjectRuntimeError::new(format!(
                    "active project '{}' has altered source metadata for {slug:?}",
                    path.display()
                )));
            }
        }
        let expected = self.expected_id()?;
        if self.generation_id != expected {
            return Err(ProjectRuntimeError::new(format!(
                "active project '{}' has an invalid generation id",
                path.display()
            )));
        }
        Ok(())
    }

    fn expected_id(&self) -> Result<String, ProjectRuntimeError> {
        let value = serde_json::json!({
            "schema": self.schema,
            "version": self.version,
            "project_root": self.project_root,
            "agents": self.agents,
        });
        let bytes = canonical_json(&value).map_err(|error| {
            ProjectRuntimeError::new(format!("cannot calculate project generation id: {error}"))
        })?;
        Ok(format!("project:{}", hash_bytes(&bytes)))
    }
}

fn write_atomic(path: &Path, bytes: &[u8]) -> Result<(), ProjectRuntimeError> {
    let parent = path.parent().ok_or_else(|| {
        ProjectRuntimeError::new(format!(
            "active project path has no parent: {}",
            path.display()
        ))
    })?;
    let parent_metadata = fs::metadata(parent).map_err(|error| {
        ProjectRuntimeError::new(format!(
            "cannot inspect active project directory '{}': {error}",
            parent.display()
        ))
    })?;
    if !parent_metadata.is_dir() {
        return Err(ProjectRuntimeError::new(format!(
            "active project parent is not a directory: {}",
            parent.display()
        )));
    }
    match fs::symlink_metadata(path) {
        Ok(metadata) if metadata.file_type().is_symlink() || !metadata.is_file() => {
            return Err(ProjectRuntimeError::new(format!(
                "active project path is not a regular file: {}",
                path.display()
            )))
        }
        Ok(_metadata) => {}
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
        Err(error) => {
            return Err(ProjectRuntimeError::new(format!(
                "cannot inspect active project '{}': {error}",
                path.display()
            )))
        }
    }

    let name = path
        .file_name()
        .and_then(|name| name.to_str())
        .ok_or_else(|| {
            ProjectRuntimeError::new(format!(
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
                return Err(ProjectRuntimeError::new(format!(
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
            ProjectRuntimeError::new(format!(
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
        ProjectRuntimeError::new(format!(
            "cannot activate project '{}': {error}",
            path.display()
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
    fn load_builds_a_stable_generation_and_filters_disabled_agents() {
        let temporary = TempDir::new().expect("temporary directory");
        write_agent(temporary.path(), "zulu", true);
        write_agent(temporary.path(), "alpha", false);

        let generation = ProjectGeneration::load(temporary.path()).expect("project loads");
        assert!(generation.generation_id().starts_with("project:b3:"));
        assert_eq!(
            generation.status().agents,
            vec![ActiveAgent {
                slug: "zulu".to_owned(),
                name: "zulu".to_owned(),
                description: "Reports current state.".to_owned(),
                schedule_count: 0,
                source_address: generation.agents["zulu"].content_address.to_string(),
            }]
        );
    }

    #[test]
    fn write_and_read_preserve_the_exact_generation() {
        let temporary = TempDir::new().expect("temporary directory");
        write_agent(temporary.path(), "worker", true);
        let generation = ProjectGeneration::load(temporary.path()).expect("project loads");
        let state = temporary.path().join("state");
        fs::create_dir(&state).expect("state directory");
        let active = state.join("active-project.json");

        generation.write(&active).expect("project writes");
        let restored = ProjectGeneration::read(&active)
            .expect("project reads")
            .expect("active project exists");
        assert_eq!(restored, generation);
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

        let error = ProjectGeneration::load(temporary.path()).expect_err("link must fail");
        assert!(error.to_string().contains("symbolic link"));
    }

    #[test]
    fn routes_preserve_agent_session_locks_and_generation() {
        let temporary = TempDir::new().expect("temporary directory");
        let agents = temporary.path().join("agents");
        fs::create_dir(&agents).expect("agents directory");
        fs::write(
            agents.join("worker.md"),
            "---\nname: Worker\ndescription: Routes work.\nsession: shared\naccepts: [example.created]\nlocks: [account]\n---\nRoute work.\n",
        )
        .expect("agent source");
        let generation = ProjectGeneration::load(temporary.path()).expect("project loads");
        let routes = generation.routes().expect("routes compile");
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
            decisions[0].project_generation(),
            Some(generation.generation_id())
        );
    }
}
