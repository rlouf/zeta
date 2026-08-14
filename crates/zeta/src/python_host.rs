//! Supervises the private Python provider host for one project environment.

use std::collections::BTreeMap;
use std::path::Path;

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use zeta_agent::{AbortSignal, AgentError};

use crate::{ProcessExecutor, ProcessExecutorConfig, ProcessLaunch};

/// Configures one Python provider host process.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PythonProviderHostConfig {
    /// Names the Python executable for the project environment.
    pub python: String,
    /// Adds or replaces environment values for the host process.
    pub environment: BTreeMap<String, String>,
    /// Bounds the provider host lifecycle.
    pub process: ProcessExecutorConfig,
}

impl PythonProviderHostConfig {
    /// Selects the project virtual environment when it exists.
    pub fn for_project(project_root: &Path) -> Self {
        let candidate = project_root.join(".venv").join("bin").join("python");
        let python = if candidate.is_file() {
            candidate.display().to_string()
        } else {
            "python3".to_owned()
        };
        Self {
            python,
            environment: BTreeMap::new(),
            process: ProcessExecutorConfig::default(),
        }
    }
}

/// Identifies the declaration source selected for one provider.
#[derive(Clone, Debug, Deserialize, PartialEq, Eq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct PythonProviderSource {
    /// Names the Python module that supplied the provider.
    pub module: String,
    /// Contains the local module path when the provider is project-local.
    pub path: Option<String>,
    /// Names the installed distribution when the provider came from a package.
    pub distribution: Option<String>,
}

/// Describes one provider selected by the Python host.
#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct PythonProvider {
    /// Carries the provider identifier.
    pub id: String,
    /// Identifies the selected declaration source.
    pub source: PythonProviderSource,
    /// Identifies the declaration and implementation bytes.
    pub fingerprint: String,
    /// Carries model-specific tool adaptations when the provider is a model.
    pub tool_profile: Option<Map<String, Value>>,
    /// Carries the canonical provider input schema when one was declared.
    pub input_schema: Option<Map<String, Value>>,
    /// Carries the provider output schema when one was declared.
    pub output_schema: Option<Map<String, Value>>,
}

/// Contains the selected Python providers for one project revision.
#[derive(Clone, Debug, Default, Deserialize, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct PythonProviderCatalog {
    models: BTreeMap<String, PythonProvider>,
    tools: BTreeMap<String, PythonProvider>,
    connectors: BTreeMap<String, PythonProvider>,
}

impl PythonProviderCatalog {
    /// Returns the selected model providers by identifier.
    pub fn models(&self) -> &BTreeMap<String, PythonProvider> {
        &self.models
    }

    /// Returns the selected tool providers by identifier.
    pub fn tools(&self) -> &BTreeMap<String, PythonProvider> {
        &self.tools
    }

    /// Returns the selected connector providers by identifier.
    pub fn connectors(&self) -> &BTreeMap<String, PythonProvider> {
        &self.connectors
    }
}

/// Owns one supervised Python provider host and its verified catalog.
pub struct PythonProviderHost {
    executor: ProcessExecutor,
    catalog: PythonProviderCatalog,
}

impl PythonProviderHost {
    /// Starts a Python provider host with the project-default interpreter.
    ///
    /// # Errors
    ///
    /// Returns [`AgentError`] when the host cannot start or returns an invalid
    /// provider catalog.
    pub fn start(project_root: impl AsRef<Path>) -> Result<Self, AgentError> {
        let project_root = project_root.as_ref();
        Self::with_config(
            project_root,
            PythonProviderHostConfig::for_project(project_root),
        )
    }

    /// Starts a Python provider host with explicit launch configuration.
    ///
    /// # Errors
    ///
    /// Returns [`AgentError`] when the host cannot start or returns an invalid
    /// provider catalog.
    pub fn with_config(
        project_root: impl AsRef<Path>,
        config: PythonProviderHostConfig,
    ) -> Result<Self, AgentError> {
        let project_root = project_root.as_ref().to_path_buf();
        let launch = ProcessLaunch {
            extension_id: "zeta-python-host".to_owned(),
            argv: vec![
                config.python,
                "-m".to_owned(),
                "zeta_plugin.host".to_owned(),
                "--project-root".to_owned(),
                project_root.display().to_string(),
            ],
            working_directory: Some(project_root),
            environment: config.environment,
        };
        let mut executor = ProcessExecutor::with_config(launch, config.process)?;
        let catalog = executor.call("providers.catalog", Map::new(), None, None, &InactiveAbort)?;
        let catalog = parse_catalog(catalog)?;
        Ok(Self { executor, catalog })
    }

    /// Returns the catalog selected at host initialization.
    pub fn catalog(&self) -> &PythonProviderCatalog {
        &self.catalog
    }

    /// Invokes a registered Python tool provider.
    ///
    /// # Errors
    ///
    /// Returns [`AgentError`] when the tool is absent or the provider fails.
    pub fn invoke(
        &mut self,
        tool: &str,
        request: Map<String, Value>,
        base_directory: Option<String>,
        effect_key: Option<String>,
        abort: &dyn AbortSignal,
    ) -> Result<Map<String, Value>, AgentError> {
        self.call(
            "invoke",
            "tool",
            tool,
            request,
            base_directory,
            effect_key,
            abort,
        )
    }

    /// Calls a registered Python model provider.
    ///
    /// # Errors
    ///
    /// Returns [`AgentError`] when the model is absent or the provider fails.
    pub fn generate(
        &mut self,
        model: &str,
        request: Map<String, Value>,
        abort: &dyn AbortSignal,
    ) -> Result<Map<String, Value>, AgentError> {
        self.call("generate", "model", model, request, None, None, abort)
    }

    /// Delivers one connector effect through a Python provider.
    ///
    /// # Errors
    ///
    /// Returns [`AgentError`] when the connector is absent or the provider fails.
    pub fn deliver(
        &mut self,
        connector: &str,
        request: Map<String, Value>,
        effect_key: Option<String>,
        abort: &dyn AbortSignal,
    ) -> Result<Map<String, Value>, AgentError> {
        self.call(
            "deliver",
            "connector",
            connector,
            request,
            None,
            effect_key,
            abort,
        )
    }

    /// Subscribes through a Python connector provider.
    ///
    /// # Errors
    ///
    /// Returns [`AgentError`] when the connector is absent or the provider fails.
    pub fn subscribe(
        &mut self,
        connector: &str,
        request: Map<String, Value>,
        abort: &dyn AbortSignal,
    ) -> Result<Map<String, Value>, AgentError> {
        self.call(
            "subscribe",
            "connector",
            connector,
            request,
            None,
            None,
            abort,
        )
    }

    fn call(
        &mut self,
        method: &str,
        provider_field: &str,
        provider: &str,
        request: Map<String, Value>,
        base_directory: Option<String>,
        effect_key: Option<String>,
        abort: &dyn AbortSignal,
    ) -> Result<Map<String, Value>, AgentError> {
        let mut input = Map::new();
        input.insert(
            provider_field.to_owned(),
            Value::String(provider.to_owned()),
        );
        input.insert("request".to_owned(), Value::Object(request));
        self.executor
            .call(method, input, base_directory, effect_key, abort)
    }
}

impl Drop for PythonProviderHost {
    fn drop(&mut self) {
        let _result = self.executor.shutdown();
    }
}

struct InactiveAbort;

impl AbortSignal for InactiveAbort {
    fn reason(&self) -> Option<zeta_agent::AbortReason> {
        None
    }
}

fn parse_catalog(value: Map<String, Value>) -> Result<PythonProviderCatalog, AgentError> {
    let mut values = value;
    let models = parse_category(&mut values, "models")?;
    let tools = parse_category(&mut values, "tools")?;
    let connectors = parse_category(&mut values, "connectors")?;
    if !values.is_empty() {
        return Err(AgentError::tool(
            "Python provider catalog has unknown fields",
        ));
    }
    Ok(PythonProviderCatalog {
        models,
        tools,
        connectors,
    })
}

fn parse_category(
    values: &mut Map<String, Value>,
    category: &str,
) -> Result<BTreeMap<String, PythonProvider>, AgentError> {
    let Some(Value::Array(entries)) = values.remove(category) else {
        return Err(AgentError::tool(format!(
            "Python provider catalog field {category:?} must be an array"
        )));
    };
    let mut providers = BTreeMap::new();
    for entry in entries {
        let provider = serde_json::from_value::<PythonProvider>(entry).map_err(|error| {
            AgentError::tool(format!(
                "Python provider catalog has an invalid {category} entry: {error}"
            ))
        })?;
        if provider.id.is_empty() || providers.insert(provider.id.clone(), provider).is_some() {
            return Err(AgentError::tool(format!(
                "Python provider catalog has a duplicate or empty {category} identifier"
            )));
        }
    }
    Ok(providers)
}
