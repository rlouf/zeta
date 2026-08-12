//! Defines authored declarations for Zeta agent systems.
//!
//! Callers supply exact agent and skill bytes plus immutable connector,
//! capability, executor-provider, model, and implementation declarations. The
//! crate validates and normalizes the whole project, then constructs strict
//! content-addressed project and per-agent execution manifests. Filesystem
//! discovery, process launch, model calls, execution, and persistence remain
//! host concerns.

mod connector;
mod error;
mod event;
mod manifest;
mod parse;
mod project;
mod spec;

pub use error::{AuthoringError, AuthoringErrorKind, SpecError, SpecErrorKind};
pub use parse::{
    load_agent, parse_agent, parse_skill, render_prompt, validate_prompt, SkillResource, SkillSpec,
};
pub use spec::{
    compile_project, derive_returns_schema, execution_manifest, matches, parse_connector,
    project_manifest, restore_execution_manifest, restore_project_manifest, scheduled_event_type,
    validate_agent, verify_execution_manifest, verify_project_manifest, AgentProject,
    AgentProjectInput, AgentSpec, AgentValidationContext, CapabilityId, CapabilitySpec,
    ConnectorOperation, ConnectorSpec, DeliverySemantics, EgressBinding, EventRegistry,
    ExecutionManifest, ExecutionManifestId, ExecutorProviderSpec, ExecutorSpec,
    ImplementationFingerprint, IngressBinding, ModelSelectionSpec, ModelSpec, ProjectGenerationId,
    ProjectManifest, RetrySpec, ScheduleEntry, EXECUTION_MANIFEST_SCHEMA,
    EXECUTION_MANIFEST_VERSION, PROJECT_MANIFEST_SCHEMA, PROJECT_MANIFEST_VERSION,
};
