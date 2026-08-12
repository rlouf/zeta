//! Constructs and verifies content-addressed authoring manifests.

use std::collections::{BTreeMap, BTreeSet};
use std::fmt;

use serde::de::Error as _;
use serde::{Deserialize, Deserializer, Serialize, Serializer};
use serde_json::Value;
use zeta_substrate::{canonical_json, hash_bytes, Hash};

use crate::connector::ConnectorSpec;
use crate::error::{AuthoringError, AuthoringErrorKind};
use crate::event::EventRegistry;
use crate::parse::{SkillResource, SkillSpec};
use crate::project::{compile_project, AgentProject, AgentProjectInput};
use crate::spec::{
    AgentSpec, CapabilitySpec, ExecutorProviderSpec, ImplementationFingerprint, ModelSelectionSpec,
};

/// Names the version 1 project-manifest schema.
pub const PROJECT_MANIFEST_SCHEMA: &str = "zeta.project";
/// Names the version 1 execution-manifest schema.
pub const EXECUTION_MANIFEST_SCHEMA: &str = "zeta.execution";
/// Selects the only supported project-manifest version.
pub const PROJECT_MANIFEST_VERSION: u64 = 1;
/// Selects the only supported execution-manifest version.
pub const EXECUTION_MANIFEST_VERSION: u64 = 1;

/// Identifies one canonical normalized project generation.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub struct ProjectGenerationId(Hash);

impl ProjectGenerationId {
    /// Returns the underlying canonical-body content address.
    pub fn as_hash(&self) -> Hash {
        self.0
    }
}

impl fmt::Display for ProjectGenerationId {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "project:{}", self.0)
    }
}

impl Serialize for ProjectGenerationId {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        serializer.serialize_str(&self.to_string())
    }
}

impl<'de> Deserialize<'de> for ProjectGenerationId {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        let text = String::deserialize(deserializer)?;
        let Some(hash) = text.strip_prefix("project:") else {
            return Err(D::Error::custom(
                "project generation id must start with project:",
            ));
        };
        let hash = hash
            .parse::<Hash>()
            .map_err(|error| D::Error::custom(error.to_string()))?;
        Ok(ProjectGenerationId(hash))
    }
}

/// Identifies one canonical per-agent execution manifest.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub struct ExecutionManifestId(Hash);

impl ExecutionManifestId {
    /// Returns the underlying canonical-body content address.
    pub fn as_hash(&self) -> Hash {
        self.0
    }
}

impl fmt::Display for ExecutionManifestId {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "execution_manifest:{}", self.0)
    }
}

impl Serialize for ExecutionManifestId {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        serializer.serialize_str(&self.to_string())
    }
}

impl<'de> Deserialize<'de> for ExecutionManifestId {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        let text = String::deserialize(deserializer)?;
        let Some(hash) = text.strip_prefix("execution_manifest:") else {
            return Err(D::Error::custom(
                "execution manifest id must start with execution_manifest:",
            ));
        };
        let hash = hash
            .parse::<Hash>()
            .map_err(|error| D::Error::custom(error.to_string()))?;
        Ok(ExecutionManifestId(hash))
    }
}

/// Serializes one complete normalized project as a versioned audit value.
#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ProjectManifest {
    /// Identifies the canonical manifest body.
    pub id: ProjectGenerationId,
    /// Names the manifest schema.
    pub schema: String,
    /// Selects the manifest schema version.
    pub version: u64,
    /// Carries normalized agents in slug order.
    pub agents: BTreeMap<String, AgentSpec>,
    /// Carries the complete merged event vocabulary.
    pub events: EventRegistry,
    /// Carries flat project skill resources.
    pub skill_resources: BTreeMap<String, SkillResource>,
    /// Carries parsed `SKILL.md` declarations.
    pub skill_specs: BTreeMap<String, SkillSpec>,
    /// Carries language-neutral connector declarations.
    pub connectors: BTreeMap<String, ConnectorSpec>,
    /// Carries canonical capability declarations.
    pub capabilities: BTreeMap<String, CapabilitySpec>,
    /// Carries executor-provider declarations.
    pub executor_providers: BTreeMap<String, ExecutorProviderSpec>,
    /// Carries the resolved model selection.
    pub model: Option<ModelSelectionSpec>,
    /// Identifies the compiling runtime implementation.
    pub runtime_fingerprint: ImplementationFingerprint,
}

#[derive(Clone, Debug, PartialEq, Serialize)]
struct ProjectManifestBody {
    schema: String,
    version: u64,
    agents: BTreeMap<String, AgentSpec>,
    events: EventRegistry,
    skill_resources: BTreeMap<String, SkillResource>,
    skill_specs: BTreeMap<String, SkillSpec>,
    connectors: BTreeMap<String, ConnectorSpec>,
    capabilities: BTreeMap<String, CapabilitySpec>,
    executor_providers: BTreeMap<String, ExecutorProviderSpec>,
    model: Option<ModelSelectionSpec>,
    runtime_fingerprint: ImplementationFingerprint,
}

impl ProjectManifest {
    fn body(&self) -> ProjectManifestBody {
        ProjectManifestBody {
            schema: self.schema.clone(),
            version: self.version,
            agents: self.agents.clone(),
            events: self.events.clone(),
            skill_resources: self.skill_resources.clone(),
            skill_specs: self.skill_specs.clone(),
            connectors: self.connectors.clone(),
            capabilities: self.capabilities.clone(),
            executor_providers: self.executor_providers.clone(),
            model: self.model.clone(),
            runtime_fingerprint: self.runtime_fingerprint.clone(),
        }
    }
}

/// Serializes the declaration projection needed to execute one agent.
#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ExecutionManifest {
    /// Identifies the canonical execution-manifest body.
    pub id: ExecutionManifestId,
    /// Names the manifest schema.
    pub schema: String,
    /// Selects the manifest schema version.
    pub version: u64,
    /// Identifies the complete project generation.
    pub project_generation: ProjectGenerationId,
    /// Carries the selected normalized agent.
    pub agent: AgentSpec,
    /// Carries only event declarations referenced by the agent.
    pub events: EventRegistry,
    /// Carries selected flat skill resources.
    pub skill_resources: BTreeMap<String, SkillResource>,
    /// Carries selected parsed `SKILL.md` declarations.
    pub skill_specs: BTreeMap<String, SkillSpec>,
    /// Carries selected canonical capabilities.
    pub capabilities: BTreeMap<String, CapabilitySpec>,
    /// Carries connectors owning relevant events.
    pub connectors: BTreeMap<String, ConnectorSpec>,
    /// Carries the selected executor-provider declaration.
    pub executor_provider: ExecutorProviderSpec,
    /// Carries the project model selection.
    pub model: Option<ModelSelectionSpec>,
    /// Identifies the compiling runtime implementation.
    pub runtime_fingerprint: ImplementationFingerprint,
}

#[derive(Clone, Debug, PartialEq, Serialize)]
struct ExecutionManifestBody {
    schema: String,
    version: u64,
    project_generation: ProjectGenerationId,
    agent: AgentSpec,
    events: EventRegistry,
    skill_resources: BTreeMap<String, SkillResource>,
    skill_specs: BTreeMap<String, SkillSpec>,
    capabilities: BTreeMap<String, CapabilitySpec>,
    connectors: BTreeMap<String, ConnectorSpec>,
    executor_provider: ExecutorProviderSpec,
    model: Option<ModelSelectionSpec>,
    runtime_fingerprint: ImplementationFingerprint,
}

impl ExecutionManifest {
    fn body(&self) -> ExecutionManifestBody {
        ExecutionManifestBody {
            schema: self.schema.clone(),
            version: self.version,
            project_generation: self.project_generation,
            agent: self.agent.clone(),
            events: self.events.clone(),
            skill_resources: self.skill_resources.clone(),
            skill_specs: self.skill_specs.clone(),
            capabilities: self.capabilities.clone(),
            connectors: self.connectors.clone(),
            executor_provider: self.executor_provider.clone(),
            model: self.model.clone(),
            runtime_fingerprint: self.runtime_fingerprint.clone(),
        }
    }
}

/// Constructs a version 1 manifest and its canonical project identity.
///
/// # Errors
///
/// Returns [`AuthoringError`] if a declaration cannot be represented by the
/// substrate canonical JSON profile.
pub fn project_manifest(project: &AgentProject) -> Result<ProjectManifest, AuthoringError> {
    let body = ProjectManifestBody {
        schema: PROJECT_MANIFEST_SCHEMA.to_owned(),
        version: PROJECT_MANIFEST_VERSION,
        agents: project.agents.clone(),
        events: project.events.clone(),
        skill_resources: project.skill_resources.clone(),
        skill_specs: project.skill_specs.clone(),
        connectors: project.connectors.clone(),
        capabilities: project.capabilities.clone(),
        executor_providers: project.executor_providers.clone(),
        model: project.model.clone(),
        runtime_fingerprint: project.runtime_fingerprint.clone(),
    };
    let id = ProjectGenerationId(manifest_hash(&body, "project")?);
    Ok(ProjectManifest {
        id,
        schema: body.schema,
        version: body.version,
        agents: body.agents,
        events: body.events,
        skill_resources: body.skill_resources,
        skill_specs: body.skill_specs,
        connectors: body.connectors,
        capabilities: body.capabilities,
        executor_providers: body.executor_providers,
        model: body.model,
        runtime_fingerprint: body.runtime_fingerprint,
    })
}

/// Constructs the relevant declaration projection for one normalized agent.
///
/// # Errors
///
/// Returns [`AuthoringError`] when the project generation does not match,
/// the agent is unknown, or a normalized reference cannot be projected.
pub fn execution_manifest(
    project: &AgentProject,
    generation: &ProjectGenerationId,
    agent_slug: &str,
) -> Result<ExecutionManifest, AuthoringError> {
    let current = project_manifest(project)?;
    if &current.id != generation {
        return Err(AuthoringError::new(
            AuthoringErrorKind::InvalidIdentity,
            Some(agent_slug),
            Some("project_generation"),
            "project generation does not identify the supplied project",
        ));
    }
    let Some(agent) = project.agents.get(agent_slug) else {
        return Err(AuthoringError::new(
            AuthoringErrorKind::UnknownAgent,
            Some(agent_slug),
            None,
            "cannot build an execution manifest for an unknown agent",
        ));
    };

    let mut relevant_event_types = BTreeSet::new();
    for event_type in &agent.accepts {
        relevant_event_types.insert(event_type.clone());
    }
    for event_type in &agent.publishes {
        relevant_event_types.insert(event_type.clone());
    }
    for event_type in &agent.returns {
        relevant_event_types.insert(event_type.clone());
    }
    let mut events = EventRegistry::new();
    for event_type in &relevant_event_types {
        let Some(schema) = project.events.schema(event_type) else {
            return Err(AuthoringError::new(
                AuthoringErrorKind::UnknownEvent,
                Some(event_type),
                None,
                "normalized agent references an absent event",
            ));
        };
        events.register(event_type, schema.cloned())?;
    }

    let mut skill_resources = BTreeMap::new();
    let mut skill_specs = BTreeMap::new();
    for name in &agent.skills {
        if let Some(skill) = project.skill_resources.get(name) {
            skill_resources.insert(name.clone(), skill.clone());
            continue;
        }
        if let Some(skill) = project.skill_specs.get(name) {
            skill_specs.insert(name.clone(), skill.clone());
            continue;
        }
        return Err(AuthoringError::new(
            AuthoringErrorKind::UnknownSkill,
            Some(agent_slug),
            Some("skills"),
            format!("normalized agent references absent skill {name:?}"),
        ));
    }

    let mut capabilities = BTreeMap::new();
    for id in &agent.tools {
        let Some(capability) = project.capabilities.get(id) else {
            return Err(AuthoringError::new(
                AuthoringErrorKind::UnknownTool,
                Some(agent_slug),
                Some("tools"),
                format!("normalized agent references absent capability {id:?}"),
            ));
        };
        capabilities.insert(id.clone(), capability.clone());
    }

    let mut connectors = BTreeMap::new();
    for (id, connector) in &project.connectors {
        let mut relevant = false;
        for event_type in &relevant_event_types {
            if connector.events.knows(event_type) {
                relevant = true;
                break;
            }
        }
        if relevant {
            connectors.insert(id.clone(), connector.clone());
        }
    }

    let Some(executor_provider) = project
        .executor_providers
        .get(&agent.executor.provider)
        .cloned()
    else {
        return Err(AuthoringError::new(
            AuthoringErrorKind::UnknownExecutorProvider,
            Some(agent_slug),
            Some("executor"),
            "normalized agent references an absent executor provider",
        ));
    };
    let body = ExecutionManifestBody {
        schema: EXECUTION_MANIFEST_SCHEMA.to_owned(),
        version: EXECUTION_MANIFEST_VERSION,
        project_generation: *generation,
        agent: agent.clone(),
        events,
        skill_resources,
        skill_specs,
        capabilities,
        connectors,
        executor_provider,
        model: project.model.clone(),
        runtime_fingerprint: project.runtime_fingerprint.clone(),
    };
    let id = ExecutionManifestId(manifest_hash(&body, "execution manifest")?);
    Ok(ExecutionManifest {
        id,
        schema: body.schema,
        version: body.version,
        project_generation: body.project_generation,
        agent: body.agent,
        events: body.events,
        skill_resources: body.skill_resources,
        skill_specs: body.skill_specs,
        capabilities: body.capabilities,
        connectors: body.connectors,
        executor_provider: body.executor_provider,
        model: body.model,
        runtime_fingerprint: body.runtime_fingerprint,
    })
}

/// Verifies one typed project manifest's schema, version, nested skill
/// identities, normalization, and canonical identity.
///
/// # Errors
///
/// Returns [`AuthoringError`] when verification fails.
pub fn verify_project_manifest(manifest: &ProjectManifest) -> Result<(), AuthoringError> {
    compile_project_manifest(manifest)?;
    Ok(())
}

/// Restores and recompiles a strict version 1 project manifest.
///
/// # Errors
///
/// Returns [`AuthoringError`] for unknown fields, invalid nested declarations,
/// non-normalized values, or an identity mismatch.
pub fn restore_project_manifest(
    value: &Value,
) -> Result<(ProjectManifest, AgentProject), AuthoringError> {
    let manifest: ProjectManifest = serde_json::from_value(value.clone()).map_err(|error| {
        AuthoringError::new(
            AuthoringErrorKind::InvalidManifest,
            None,
            None,
            error.to_string(),
        )
    })?;
    let project = compile_project_manifest(&manifest)?;
    Ok((manifest, project))
}

fn compile_project_manifest(manifest: &ProjectManifest) -> Result<AgentProject, AuthoringError> {
    validate_manifest_header(
        &manifest.schema,
        manifest.version,
        PROJECT_MANIFEST_SCHEMA,
        PROJECT_MANIFEST_VERSION,
        "project",
    )?;
    let mut agents = Vec::new();
    for spec in manifest.agents.values() {
        if hash_bytes(spec.source.as_bytes()) != spec.content_address {
            return Err(AuthoringError::new(
                AuthoringErrorKind::InvalidIdentity,
                Some(&spec.slug),
                Some("content_address"),
                "agent source content address does not match its exact source",
            ));
        }
        let agent =
            crate::parse::parse_agent(&spec.slug, spec.source.as_bytes()).map_err(|error| {
                AuthoringError::new(
                    AuthoringErrorKind::InvalidAgent,
                    Some(&spec.slug),
                    Some("source"),
                    error.to_string(),
                )
            })?;
        agents.push(agent);
    }
    let input = AgentProjectInput {
        agents,
        events: manifest.events.clone(),
        skill_resources: manifest.skill_resources.values().cloned().collect(),
        skill_specs: manifest.skill_specs.values().cloned().collect(),
        connectors: manifest.connectors.values().cloned().collect(),
        capabilities: manifest.capabilities.values().cloned().collect(),
        executor_providers: manifest.executor_providers.values().cloned().collect(),
        model: manifest.model.clone(),
        runtime_fingerprint: manifest.runtime_fingerprint.clone(),
    };
    let project = compile_project(input)?;
    let rebuilt = project_manifest(&project)?;
    if rebuilt.body() != manifest.body() {
        return Err(AuthoringError::new(
            AuthoringErrorKind::InvalidManifest,
            Some(&manifest.id.to_string()),
            None,
            "project manifest is valid but not normalized",
        ));
    }
    if rebuilt.id != manifest.id {
        return Err(AuthoringError::new(
            AuthoringErrorKind::InvalidIdentity,
            Some(&manifest.id.to_string()),
            Some("id"),
            format!("project manifest identity must be {}", rebuilt.id),
        ));
    }
    Ok(project)
}

/// Verifies one execution manifest against its complete project manifest.
///
/// # Errors
///
/// Returns [`AuthoringError`] when either manifest is invalid or the execution
/// projection differs from the canonical per-agent projection.
pub fn verify_execution_manifest(
    manifest: &ExecutionManifest,
    project_manifest_value: &ProjectManifest,
) -> Result<(), AuthoringError> {
    verify_project_manifest(project_manifest_value)?;
    validate_manifest_header(
        &manifest.schema,
        manifest.version,
        EXECUTION_MANIFEST_SCHEMA,
        EXECUTION_MANIFEST_VERSION,
        "execution",
    )?;
    let expected_id = ExecutionManifestId(manifest_hash(&manifest.body(), "execution manifest")?);
    if manifest.id != expected_id {
        return Err(AuthoringError::new(
            AuthoringErrorKind::InvalidIdentity,
            Some(&manifest.id.to_string()),
            Some("id"),
            format!("execution manifest identity must be {expected_id}"),
        ));
    }
    if manifest.project_generation != project_manifest_value.id {
        return Err(AuthoringError::new(
            AuthoringErrorKind::InvalidIdentity,
            Some(&manifest.id.to_string()),
            Some("project_generation"),
            "execution manifest names a different project generation",
        ));
    }
    let value = serde_json::to_value(project_manifest_value).map_err(|error| {
        AuthoringError::new(
            AuthoringErrorKind::InvalidManifest,
            None,
            None,
            error.to_string(),
        )
    })?;
    let (_manifest, project) = restore_project_manifest(&value)?;
    let expected = execution_manifest(&project, &project_manifest_value.id, &manifest.agent.slug)?;
    if &expected != manifest {
        return Err(AuthoringError::new(
            AuthoringErrorKind::InvalidManifest,
            Some(&manifest.id.to_string()),
            None,
            "execution manifest is not the canonical project projection",
        ));
    }
    Ok(())
}

/// Restores and verifies a strict execution manifest value.
///
/// # Errors
///
/// Returns [`AuthoringError`] for unknown fields, malformed values, identity
/// mismatches, or a non-canonical project projection.
pub fn restore_execution_manifest(
    value: &Value,
    project: &ProjectManifest,
) -> Result<ExecutionManifest, AuthoringError> {
    let manifest: ExecutionManifest = serde_json::from_value(value.clone()).map_err(|error| {
        AuthoringError::new(
            AuthoringErrorKind::InvalidManifest,
            None,
            None,
            error.to_string(),
        )
    })?;
    verify_execution_manifest(&manifest, project)?;
    Ok(manifest)
}

fn manifest_hash(value: &impl Serialize, subject: &str) -> Result<Hash, AuthoringError> {
    let value = serde_json::to_value(value).map_err(|error| {
        AuthoringError::new(
            AuthoringErrorKind::InvalidIdentity,
            Some(subject),
            None,
            error.to_string(),
        )
    })?;
    let bytes = canonical_json(&value).map_err(|error| {
        AuthoringError::new(
            AuthoringErrorKind::InvalidIdentity,
            Some(subject),
            None,
            error.to_string(),
        )
    })?;
    Ok(hash_bytes(&bytes))
}

fn validate_manifest_header(
    schema: &str,
    version: u64,
    expected_schema: &str,
    expected_version: u64,
    subject: &str,
) -> Result<(), AuthoringError> {
    if schema != expected_schema || version != expected_version {
        return Err(AuthoringError::new(
            AuthoringErrorKind::InvalidManifest,
            Some(subject),
            None,
            format!(
                "expected schema {expected_schema:?} version {expected_version}, got {schema:?} version {version}"
            ),
        ));
    }
    Ok(())
}
