//! Compiles authored declarations into normalized agent projects.

use std::collections::{BTreeMap, BTreeSet};

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use zeta_substrate::hash_bytes;

use crate::connector::{validate_connector_spec, ConnectorSpec};
use crate::error::{ManifestError, ManifestErrorKind};
use crate::event::{validate_schema, EventRegistry};
use crate::parse::{validate_prompt, SkillResource, SkillSpec};
use crate::spec::{
    AgentSpec, CapabilityId, CapabilitySpec, ExecutorProviderSpec, ImplementationFingerprint,
    ModelSelectionSpec,
};

const RESERVED_TOOL_NAMES: [&str; 6] = [
    "publish_event",
    "zeta.publish_event",
    "wait_for",
    "zeta.wait_for",
    "cancel",
    "zeta.cancel",
];

/// Collects already-discovered declarations for pure project compilation.
///
/// The host supplies exact values and retains filesystem discovery, process
/// metadata, credentials, and runtime state.
#[derive(Clone, Debug, PartialEq)]
pub struct AgentProjectInput {
    /// Carries parsed authored agents.
    pub agents: Vec<AgentSpec>,
    /// Carries local event declarations.
    pub events: EventRegistry,
    /// Carries flat project skill resources.
    pub skill_resources: Vec<SkillResource>,
    /// Carries parsed `SKILL.md` declarations.
    pub skill_specs: Vec<SkillSpec>,
    /// Carries language-neutral connector descriptions.
    pub connectors: Vec<ConnectorSpec>,
    /// Carries host-supplied capabilities.
    pub capabilities: Vec<CapabilitySpec>,
    /// Carries host-supplied executor providers.
    pub executor_providers: Vec<ExecutorProviderSpec>,
    /// Carries the selected model declaration when one exists.
    pub model: Option<ModelSelectionSpec>,
    /// Identifies the native runtime implementation.
    pub runtime_fingerprint: ImplementationFingerprint,
}

/// Holds one validated and deterministically normalized authored project.
#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct AgentProject {
    /// Maps agent slugs to normalized declarations.
    pub agents: BTreeMap<String, AgentSpec>,
    /// Holds the complete merged event vocabulary.
    pub events: EventRegistry,
    /// Maps flat skill names to content-addressed resources.
    pub skill_resources: BTreeMap<String, SkillResource>,
    /// Maps parsed `SKILL.md` names to declarations.
    pub skill_specs: BTreeMap<String, SkillSpec>,
    /// Maps connector identities to language-neutral descriptions.
    pub connectors: BTreeMap<String, ConnectorSpec>,
    /// Maps canonical capability identities to declarations.
    pub capabilities: BTreeMap<String, CapabilitySpec>,
    /// Maps executor-provider identities to declarations.
    pub executor_providers: BTreeMap<String, ExecutorProviderSpec>,
    /// Carries the host-resolved model selection.
    pub model: Option<ModelSelectionSpec>,
    /// Identifies the runtime implementation used for compilation.
    pub runtime_fingerprint: ImplementationFingerprint,
}

/// Supplies optional vocabularies for validating one agent declaration.
///
/// An absent vocabulary disables only its corresponding reference checks.
/// Project compilation supplies every vocabulary and is always strict.
#[derive(Clone, Copy, Debug, Default)]
pub struct AgentValidationContext<'a> {
    /// Supplies known events when event references should be checked.
    pub events: Option<&'a EventRegistry>,
    /// Supplies known skill names when skill references should be checked.
    pub skill_names: Option<&'a BTreeSet<String>>,
    /// Supplies known capabilities when tool references should be checked.
    pub capabilities: Option<&'a BTreeMap<String, CapabilitySpec>>,
    /// Supplies connector ownership and binding schemas when bindings should be checked.
    pub connectors: Option<&'a BTreeMap<String, ConnectorSpec>>,
    /// Supplies known executor providers when provider references should be checked.
    pub executor_providers: Option<&'a BTreeMap<String, ExecutorProviderSpec>>,
}

/// Compiles supplied declarations into one validated normalized project.
///
/// # Errors
///
/// Returns [`ManifestError`] for conflicting declarations, invalid schemas,
/// unknown references, invalid bindings, or unsupported extensions.
pub fn compile_project(input: AgentProjectInput) -> Result<AgentProject, ManifestError> {
    let AgentProjectInput {
        agents,
        events: local_events,
        skill_resources,
        skill_specs,
        connectors,
        capabilities,
        executor_providers,
        model,
        runtime_fingerprint,
    } = input;

    let mut connectors_by_id = BTreeMap::new();
    let mut event_owners = BTreeMap::new();
    let mut events = EventRegistry::new();
    for connector in connectors {
        validate_connector_spec(&connector)?;
        if connectors_by_id.contains_key(&connector.id) {
            return Err(duplicate_declaration("connector", &connector.id));
        }
        for (event_type, schema) in connector.events.iter() {
            if let Some(owner) = event_owners.insert(event_type.clone(), connector.id.clone()) {
                return Err(ManifestError::new(
                    ManifestErrorKind::DuplicateDeclaration,
                    Some(event_type),
                    Some("connectors"),
                    format!(
                        "connectors {owner:?} and {:?} both own this event",
                        connector.id
                    ),
                ));
            }
            events.register(event_type, schema.clone())?;
        }
        connectors_by_id.insert(connector.id.clone(), connector);
    }
    for (event_type, schema) in local_events.iter() {
        events.register(event_type, schema.clone())?;
    }

    let mut agents_by_slug = BTreeMap::new();
    for agent in agents {
        validate_agent_declaration(&agent)?;
        verify_authored_agent(&agent)?;
        if agents_by_slug.contains_key(&agent.slug) {
            return Err(duplicate_declaration("agent", &agent.slug));
        }
        agents_by_slug.insert(agent.slug.clone(), agent);
    }

    let mut skill_resources_by_name = BTreeMap::new();
    for skill in skill_resources {
        validate_skill_resource(&skill)?;
        if skill_resources_by_name.contains_key(&skill.name) {
            return Err(duplicate_declaration("skill", &skill.name));
        }
        skill_resources_by_name.insert(skill.name.clone(), skill);
    }
    let mut skill_specs_by_name = BTreeMap::new();
    for skill in skill_specs {
        validate_skill_spec(&skill)?;
        if skill_resources_by_name.contains_key(&skill.name)
            || skill_specs_by_name.contains_key(&skill.name)
        {
            return Err(duplicate_declaration("skill", &skill.name));
        }
        skill_specs_by_name.insert(skill.name.clone(), skill);
    }
    let mut skill_names = BTreeSet::new();
    for name in skill_resources_by_name.keys() {
        skill_names.insert(name.clone());
    }
    for name in skill_specs_by_name.keys() {
        skill_names.insert(name.clone());
    }

    let mut capabilities_by_id = BTreeMap::new();
    for capability in capabilities {
        validate_capability(&capability, &agents_by_slug)?;
        let id = capability.id.as_str().to_owned();
        if capabilities_by_id.contains_key(&id) {
            return Err(duplicate_declaration("capability", &id));
        }
        capabilities_by_id.insert(id, capability);
    }

    let mut executor_providers_by_id = BTreeMap::new();
    for provider in executor_providers {
        validate_executor_provider(&provider)?;
        if executor_providers_by_id.contains_key(&provider.id) {
            return Err(duplicate_declaration("executor provider", &provider.id));
        }
        executor_providers_by_id.insert(provider.id.clone(), provider);
    }
    if let Some(model) = &model {
        validate_model_selection(model)?;
    }

    for agent in agents_by_slug.values() {
        if !agent.schedules.is_empty() {
            events.register_scheduled(&agent.slug)?;
        }
    }

    for agent in agents_by_slug.values_mut() {
        normalize_agent_skills(agent, &skill_names)?;
        normalize_agent_tools(agent, &capabilities_by_id)?;
        let context = AgentValidationContext {
            events: Some(&events),
            skill_names: Some(&skill_names),
            capabilities: Some(&capabilities_by_id),
            connectors: Some(&connectors_by_id),
            executor_providers: Some(&executor_providers_by_id),
        };
        validate_agent(agent, &context)?;
    }

    Ok(AgentProject {
        agents: agents_by_slug,
        events,
        skill_resources: skill_resources_by_name,
        skill_specs: skill_specs_by_name,
        connectors: connectors_by_id,
        capabilities: capabilities_by_id,
        executor_providers: executor_providers_by_id,
        model,
        runtime_fingerprint,
    })
}

/// Validates one agent against the supplied declaration vocabularies.
///
/// # Errors
///
/// Returns [`ManifestError`] when the prompt, references, extensions, or
/// connector bindings are invalid.
pub fn validate_agent(
    spec: &AgentSpec,
    context: &AgentValidationContext<'_>,
) -> Result<(), ManifestError> {
    validate_prompt(spec)?;
    if let Some((extension, _value)) = spec.extensions.iter().next() {
        return Err(ManifestError::new(
            ManifestErrorKind::UnknownExtension,
            Some(&spec.slug),
            Some(extension),
            format!("unknown authored extension {extension:?}"),
        ));
    }

    if let Some(capabilities) = context.capabilities {
        for name in &spec.tools {
            validate_selected_tool(spec, name, capabilities)?;
        }
    } else {
        for name in &spec.tools {
            if reserved_tool(name) {
                return Err(ManifestError::new(
                    ManifestErrorKind::ReservedTool,
                    Some(&spec.slug),
                    Some("tools"),
                    format!("lists reserved tool {name:?}"),
                ));
            }
        }
    }

    if let Some(skill_names) = context.skill_names {
        for name in &spec.skills {
            if !skill_names.contains(name) {
                return Err(ManifestError::new(
                    ManifestErrorKind::UnknownSkill,
                    Some(&spec.slug),
                    Some("skills"),
                    format!("lists unknown skill {name:?}"),
                ));
            }
        }
    }
    if let Some(providers) = context.executor_providers {
        if !providers.contains_key(&spec.executor.provider) {
            return Err(ManifestError::new(
                ManifestErrorKind::UnknownExecutorProvider,
                Some(&spec.slug),
                Some("executor"),
                format!(
                    "selects unknown executor provider {:?}",
                    spec.executor.provider
                ),
            ));
        }
    }
    if let Some(events) = context.events {
        validate_event_references(spec, events)?;
    }
    if let Some(connectors) = context.connectors {
        validate_connector_bindings(spec, connectors)?;
    }
    Ok(())
}

fn duplicate_declaration(kind: &str, id: &str) -> ManifestError {
    ManifestError::new(
        ManifestErrorKind::DuplicateDeclaration,
        Some(id),
        None,
        format!("duplicate {kind} declaration"),
    )
}

fn validate_skill_resource(skill: &SkillResource) -> Result<(), ManifestError> {
    if skill.name.is_empty() {
        return Err(ManifestError::new(
            ManifestErrorKind::InvalidSkill,
            Some(&skill.name),
            Some("name"),
            "skill name must be non-empty",
        ));
    }
    let expected = SkillResource::new(&skill.name, skill.body.as_bytes())?;
    if expected.object_id != skill.object_id {
        return Err(ManifestError::new(
            ManifestErrorKind::InvalidIdentity,
            Some(&skill.name),
            Some("object_id"),
            "flat skill content address does not match its body",
        ));
    }
    Ok(())
}

fn validate_skill_spec(skill: &SkillSpec) -> Result<(), ManifestError> {
    if !valid_skill_declaration_name(&skill.name) || skill.description.trim().is_empty() {
        return Err(ManifestError::new(
            ManifestErrorKind::InvalidSkill,
            Some(&skill.name),
            None,
            "parsed skill requires a non-empty name and description",
        ));
    }
    Ok(())
}

fn validate_capability(
    capability: &CapabilitySpec,
    agents: &BTreeMap<String, AgentSpec>,
) -> Result<(), ManifestError> {
    capability.id.as_str().parse::<CapabilityId>()?;
    if reserved_tool(capability.id.as_str()) || reserved_tool(&capability.name) {
        return Err(ManifestError::new(
            ManifestErrorKind::ReservedTool,
            Some(capability.id.as_str()),
            Some("name"),
            format!(
                "capability name {:?} is reserved by the runtime",
                capability.name
            ),
        ));
    }
    if capability.name.is_empty() || capability.description.trim().is_empty() {
        return Err(ManifestError::new(
            ManifestErrorKind::InvalidCapability,
            Some(capability.id.as_str()),
            None,
            "capability name and description must be non-empty",
        ));
    }
    validate_schema(capability.id.as_str(), &capability.input_schema)?;
    let Some(owner) = &capability.owner else {
        return Ok(());
    };
    if !agents.contains_key(owner) {
        return Err(ManifestError::new(
            ManifestErrorKind::UnknownAgent,
            Some(owner),
            Some("owner"),
            format!("capability {:?} has an unknown owner", capability.id),
        ));
    }
    let prefix = format!("agent.{owner}.");
    if !capability.id.as_str().starts_with(&prefix) {
        return Err(ManifestError::new(
            ManifestErrorKind::InvalidCapability,
            Some(capability.id.as_str()),
            Some("owner"),
            format!("agent-owned capability must start with {prefix:?}"),
        ));
    }
    Ok(())
}

fn validate_agent_declaration(spec: &AgentSpec) -> Result<(), ManifestError> {
    if !valid_agent_slug(&spec.slug) || spec.name.is_empty() || spec.description.is_empty() {
        return Err(invalid_agent(
            spec,
            None,
            "agent requires a valid slug, name, and description",
        ));
    }
    if spec.session != "shared" && spec.session != "per-event" && !spec.session.contains('{') {
        return Err(invalid_agent(
            spec,
            Some("session"),
            "session must be shared, per-event, or a template",
        ));
    }
    if let Some(model) = &spec.model {
        if model.name.is_empty() || model.url.is_empty() {
            return Err(invalid_agent(
                spec,
                Some("model"),
                "agent model name and URL must be non-empty",
            ));
        }
    }
    if spec.executor.provider.is_empty() {
        return Err(invalid_agent(
            spec,
            Some("executor"),
            "executor provider must be non-empty",
        ));
    }
    let lists = [
        ("accepts", spec.accepts.as_slice()),
        ("publishes", spec.publishes.as_slice()),
        ("returns", spec.returns.as_slice()),
        ("skills", spec.skills.as_slice()),
        ("tools", spec.tools.as_slice()),
        ("locks", spec.locks.as_slice()),
    ];
    for (field, values) in lists {
        for value in values {
            if value.is_empty() {
                return Err(invalid_agent(
                    spec,
                    Some(field),
                    "declaration list values must be non-empty",
                ));
            }
        }
    }
    for schedule in &spec.schedules {
        if schedule.cron.is_empty() || schedule.timezone.as_deref() == Some("") {
            return Err(invalid_agent(
                spec,
                Some("schedules"),
                "schedule values do not match the authored profile",
            ));
        }
        if let Some(catchup) = &schedule.catchup {
            if catchup != "latest" {
                return Err(invalid_agent(
                    spec,
                    Some("schedules"),
                    "schedule values do not match the authored profile",
                ));
            }
        }
    }
    if let Some(retry) = &spec.retry {
        if retry.max_attempts == Some(0) {
            return Err(invalid_agent(
                spec,
                Some("retry"),
                "retry values do not match the authored profile",
            ));
        }
        if let Some(backoff) = retry.backoff_seconds {
            if !backoff.is_finite() || backoff < 0.0 {
                return Err(invalid_agent(
                    spec,
                    Some("retry"),
                    "retry values do not match the authored profile",
                ));
            }
        }
    }
    if let Some(base_dir) = &spec.base_dir {
        let Some(base_dir) = base_dir.to_str() else {
            return Err(invalid_agent(
                spec,
                Some("base_dir"),
                "base directory must be UTF-8",
            ));
        };
        if base_dir.trim().is_empty() {
            return Err(invalid_agent(
                spec,
                Some("base_dir"),
                "base directory must be non-empty",
            ));
        }
    }
    for binding in &spec.ingress {
        if binding.event.is_empty() || binding.idempotency_key.as_deref() == Some("") {
            return Err(invalid_agent(
                spec,
                Some("accepts"),
                "ingress binding values must be non-empty",
            ));
        }
    }
    for binding in &spec.egress {
        if binding.event.is_empty() || binding.idempotency_key.as_deref() == Some("") {
            return Err(invalid_agent(
                spec,
                Some("publishes"),
                "egress binding values must be non-empty",
            ));
        }
    }
    Ok(())
}

fn verify_authored_agent(spec: &AgentSpec) -> Result<(), ManifestError> {
    if hash_bytes(spec.source.as_bytes()) != spec.content_address {
        return Err(ManifestError::new(
            ManifestErrorKind::InvalidIdentity,
            Some(&spec.slug),
            Some("content_address"),
            "agent source content address does not match its exact source",
        ));
    }
    let parsed =
        crate::parse::parse_agent(&spec.slug, spec.source.as_bytes()).map_err(|error| {
            ManifestError::new(
                ManifestErrorKind::InvalidAgent,
                Some(&spec.slug),
                Some("source"),
                error.to_string(),
            )
        })?;
    if &parsed != spec {
        return Err(ManifestError::new(
            ManifestErrorKind::InvalidAgent,
            Some(&spec.slug),
            Some("source"),
            "typed agent declaration does not match its exact source",
        ));
    }
    Ok(())
}

fn invalid_agent(
    spec: &AgentSpec,
    field: Option<&str>,
    detail: impl Into<String>,
) -> ManifestError {
    ManifestError::new(
        ManifestErrorKind::InvalidAgent,
        Some(&spec.slug),
        field,
        detail,
    )
}

fn valid_agent_slug(slug: &str) -> bool {
    if slug.is_empty() {
        return false;
    }
    for byte in slug.bytes() {
        if !byte.is_ascii_lowercase() && !byte.is_ascii_digit() && byte != b'_' && byte != b'-' {
            return false;
        }
    }
    true
}

fn valid_skill_declaration_name(name: &str) -> bool {
    if name.is_empty() {
        return false;
    }
    for byte in name.bytes() {
        if !byte.is_ascii_lowercase() && !byte.is_ascii_digit() && byte != b'-' {
            return false;
        }
    }
    true
}

fn validate_executor_provider(provider: &ExecutorProviderSpec) -> Result<(), ManifestError> {
    let mut contains_whitespace = false;
    for character in provider.id.chars() {
        if character.is_whitespace() {
            contains_whitespace = true;
        }
    }
    if provider.id.is_empty() || contains_whitespace {
        return Err(ManifestError::new(
            ManifestErrorKind::InvalidExecutorProvider,
            Some(&provider.id),
            Some("id"),
            "executor provider id must be non-empty and contain no whitespace",
        ));
    }
    Ok(())
}

fn validate_model_selection(model: &ModelSelectionSpec) -> Result<(), ManifestError> {
    let fields = [
        ("profile", model.profile.as_str()),
        ("model", model.model.as_str()),
        ("url", model.url.as_str()),
        ("api", model.api.as_str()),
        ("tool_profile", model.tool_profile.as_str()),
    ];
    for (field, value) in fields {
        if value.trim().is_empty() {
            return Err(ManifestError::new(
                ManifestErrorKind::InvalidModel,
                Some(&model.profile),
                Some(field),
                format!("model selection {field} must be non-empty"),
            ));
        }
    }
    if model.api != "chat-completions" && model.api != "codex-responses" {
        return Err(ManifestError::new(
            ManifestErrorKind::InvalidModel,
            Some(&model.profile),
            Some("api"),
            format!("unsupported model API {:?}", model.api),
        ));
    }
    if model.tool_profile != "native" && model.tool_profile != "codex" {
        return Err(ManifestError::new(
            ManifestErrorKind::InvalidModel,
            Some(&model.profile),
            Some("tool_profile"),
            format!("unsupported model tool profile {:?}", model.tool_profile),
        ));
    }
    Ok(())
}

fn normalize_agent_skills(
    spec: &mut AgentSpec,
    skill_names: &BTreeSet<String>,
) -> Result<(), ManifestError> {
    if spec.skills_inherit {
        spec.skills = skill_names.iter().cloned().collect();
        spec.skills_inherit = false;
        return Ok(());
    }
    for name in &spec.skills {
        if !skill_names.contains(name) {
            return Err(ManifestError::new(
                ManifestErrorKind::UnknownSkill,
                Some(&spec.slug),
                Some("skills"),
                format!("lists unknown skill {name:?}"),
            ));
        }
    }
    Ok(())
}

fn normalize_agent_tools(
    spec: &mut AgentSpec,
    capabilities: &BTreeMap<String, CapabilitySpec>,
) -> Result<(), ManifestError> {
    let inherited = spec.tools_inherit;
    let mut selected = Vec::new();
    if spec.tools_inherit {
        for capability in capabilities.values() {
            if capability.owner.is_none() {
                selected.push(capability.id.as_str().to_owned());
            }
        }
        selected.sort();
    } else {
        for name in &spec.tools {
            if reserved_tool(name) {
                return Err(ManifestError::new(
                    ManifestErrorKind::ReservedTool,
                    Some(&spec.slug),
                    Some("tools"),
                    format!("lists reserved tool {name:?}"),
                ));
            }
            let capability = selected_capability(spec, name, capabilities)?;
            if reserved_tool(capability.id.as_str()) {
                return Err(ManifestError::new(
                    ManifestErrorKind::ReservedTool,
                    Some(&spec.slug),
                    Some("tools"),
                    format!("lists reserved tool {name:?}"),
                ));
            }
            if let Some(owner) = &capability.owner {
                if owner != &spec.slug {
                    return Err(ManifestError::new(
                        ManifestErrorKind::UnknownTool,
                        Some(&spec.slug),
                        Some("tools"),
                        format!("cannot select capability owned by agent {owner:?}"),
                    ));
                }
            }
            let id = capability.id.as_str().to_owned();
            if !selected.contains(&id) {
                selected.push(id);
            }
        }
    }
    let mut owned = Vec::new();
    for capability in capabilities.values() {
        if capability.owner.as_deref() == Some(&spec.slug) {
            owned.push(capability.id.as_str().to_owned());
        }
    }
    owned.sort();
    for id in owned {
        if !selected.contains(&id) {
            selected.push(id);
        }
    }
    if inherited {
        selected.sort();
    }
    spec.tools = selected;
    spec.tools_inherit = false;
    Ok(())
}

fn selected_capability<'a>(
    spec: &AgentSpec,
    name: &str,
    capabilities: &'a BTreeMap<String, CapabilitySpec>,
) -> Result<&'a CapabilitySpec, ManifestError> {
    if let Some(capability) = capabilities.get(name) {
        return Ok(capability);
    }
    let mut resolved = None;
    for capability in capabilities.values() {
        if capability.name != name {
            continue;
        }
        if resolved.is_some() {
            return Err(ManifestError::new(
                ManifestErrorKind::ConflictingDeclaration,
                Some(&spec.slug),
                Some("tools"),
                format!("tool alias {name:?} matches multiple capabilities"),
            ));
        }
        resolved = Some(capability);
    }
    let Some(resolved) = resolved else {
        return Err(ManifestError::new(
            ManifestErrorKind::UnknownTool,
            Some(&spec.slug),
            Some("tools"),
            format!("lists unknown tool {name:?}"),
        ));
    };
    Ok(resolved)
}

fn validate_selected_tool(
    spec: &AgentSpec,
    name: &str,
    capabilities: &BTreeMap<String, CapabilitySpec>,
) -> Result<(), ManifestError> {
    if reserved_tool(name) {
        return Err(ManifestError::new(
            ManifestErrorKind::ReservedTool,
            Some(&spec.slug),
            Some("tools"),
            format!("lists reserved tool {name:?}"),
        ));
    }
    let capability = selected_capability(spec, name, capabilities)?;
    if let Some(owner) = &capability.owner {
        if owner != &spec.slug {
            return Err(ManifestError::new(
                ManifestErrorKind::UnknownTool,
                Some(&spec.slug),
                Some("tools"),
                format!("cannot select capability owned by agent {owner:?}"),
            ));
        }
    }
    Ok(())
}

fn reserved_tool(name: &str) -> bool {
    RESERVED_TOOL_NAMES.contains(&name)
}

fn validate_event_references(
    spec: &AgentSpec,
    events: &EventRegistry,
) -> Result<(), ManifestError> {
    let references = [
        ("accepts", spec.accepts.as_slice()),
        ("publishes", spec.publishes.as_slice()),
        ("returns", spec.returns.as_slice()),
    ];
    for (field, event_types) in references {
        for event_type in event_types {
            if !events.knows(event_type) {
                return Err(ManifestError::new(
                    ManifestErrorKind::UnknownEvent,
                    Some(&spec.slug),
                    Some(field),
                    format!("references unknown event {event_type:?}"),
                ));
            }
        }
    }
    Ok(())
}

fn validate_connector_bindings(
    spec: &AgentSpec,
    connectors: &BTreeMap<String, ConnectorSpec>,
) -> Result<(), ManifestError> {
    for binding in &spec.ingress {
        let connector = connector_for_event(connectors, &binding.event).ok_or_else(|| {
            binding_error(
                spec,
                "accepts",
                format!("unknown ingress event {:?}", binding.event),
            )
        })?;
        if !connector.ingress_event(&binding.event) || !spec.accepts.contains(&binding.event) {
            return Err(binding_error(
                spec,
                "accepts",
                format!("event {:?} is not connector ingress", binding.event),
            ));
        }
        if binding.idempotency_key.is_none() {
            return Err(binding_error(
                spec,
                "accepts",
                format!("ingress event {:?} requires idempotency_key", binding.event),
            ));
        }
        let schema = connector
            .filters
            .get(&binding.event)
            .and_then(Option::as_ref);
        validate_binding_value(spec, "accepts", "filter", &binding.filter, schema)?;
    }
    for binding in &spec.egress {
        let connector = connector_for_event(connectors, &binding.event).ok_or_else(|| {
            binding_error(
                spec,
                "publishes",
                format!("unknown egress event {:?}", binding.event),
            )
        })?;
        let Some(operation) = connector.operations.get(&binding.event) else {
            return Err(binding_error(
                spec,
                "publishes",
                format!("event {:?} is not a connector operation", binding.event),
            ));
        };
        if !spec.publishes.contains(&binding.event) {
            return Err(binding_error(
                spec,
                "publishes",
                format!(
                    "egress event {:?} is not listed in publishes",
                    binding.event
                ),
            ));
        }
        validate_binding_value(
            spec,
            "publishes",
            "options",
            &binding.options,
            operation.options_schema.as_ref(),
        )?;
    }
    Ok(())
}

fn connector_for_event<'a>(
    connectors: &'a BTreeMap<String, ConnectorSpec>,
    event_type: &str,
) -> Option<&'a ConnectorSpec> {
    let mut owner = None;
    for connector in connectors.values() {
        if !connector.events.knows(event_type) {
            continue;
        }
        if owner.is_some() {
            return None;
        }
        owner = Some(connector);
    }
    owner
}

fn validate_binding_value(
    spec: &AgentSpec,
    field: &'static str,
    noun: &str,
    value: &Map<String, Value>,
    schema: Option<&Map<String, Value>>,
) -> Result<(), ManifestError> {
    let Some(schema) = schema else {
        if value.is_empty() {
            return Ok(());
        }
        return Err(binding_error(
            spec,
            field,
            format!("connector {noun} is not supported"),
        ));
    };
    let schema = Value::Object(schema.clone());
    let validator = jsonschema::options()
        .with_draft(jsonschema::Draft::Draft202012)
        .build(&schema)
        .map_err(|error| binding_error(spec, field, error.to_string()))?;
    let instance = Value::Object(value.clone());
    if let Err(error) = validator.validate(&instance) {
        return Err(binding_error(spec, field, error.to_string()));
    }
    Ok(())
}

fn binding_error(
    spec: &AgentSpec,
    field: &'static str,
    detail: impl Into<String>,
) -> ManifestError {
    ManifestError::new(
        ManifestErrorKind::InvalidBinding,
        Some(&spec.slug),
        Some(field),
        detail,
    )
}
