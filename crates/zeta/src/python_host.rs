//! Supervises the private Python provider host for one project environment.

use std::collections::BTreeMap;
use std::future::Future;
use std::path::Path;
use std::pin::Pin;
use std::sync::{Arc, Mutex};

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use zeta_agent::{
    AbortSignal, AgentError, AgentObserver, CapabilityExecutor, CapabilityFuture,
    CapabilityInvocation, ModelGateway, ModelInput, ModelOutput, ModelRequest, NativeToolExecutor,
};

use crate::{ExecutorBundle, ExecutorReuse, ProcessExecutor, ProcessExecutorConfig, ProcessLaunch};

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
    /// Carries the model-facing description for a tool provider.
    #[serde(default)]
    pub description: Option<String>,
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
    #[serde(default)]
    executors: BTreeMap<String, PythonProvider>,
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

    /// Returns the trusted executor drivers by identifier.
    pub fn executors(&self) -> &BTreeMap<String, PythonProvider> {
        &self.executors
    }
}

/// Owns one supervised Python provider host and its verified catalog.
pub struct PythonProviderHost {
    executor: ProcessExecutor,
    catalog: PythonProviderCatalog,
}

/// Shares one supervised Python host between model and tool adapters.
pub type SharedPythonProviderHost = Arc<Mutex<PythonProviderHost>>;

/// Generates model responses through a Python model provider.
pub struct PythonModelGateway {
    host: SharedPythonProviderHost,
    model: String,
}

impl PythonModelGateway {
    /// Creates one model gateway for a selected Python model provider.
    pub fn new(host: SharedPythonProviderHost, model: impl Into<String>) -> Self {
        Self {
            host,
            model: model.into(),
        }
    }
}

impl ModelGateway for PythonModelGateway {
    fn generate<'a>(
        &'a mut self,
        input: &'a ModelInput,
        request: &'a ModelRequest,
        observer: &'a mut dyn AgentObserver,
        abort: &'a dyn AbortSignal,
    ) -> Pin<Box<dyn Future<Output = Result<ModelOutput, AgentError>> + 'a>> {
        Box::pin(async move {
            let mut provider_request = Map::new();
            provider_request.insert("input".to_owned(), serialize_object(input, "model input")?);
            provider_request.insert(
                "model_request".to_owned(),
                serialize_object(request, "model request")?,
            );
            let mut host = self
                .host
                .lock()
                .map_err(|_error| AgentError::tool("the Python provider host is unavailable"))?;
            let result = host.generate(&self.model, provider_request, observer, abort)?;
            serde_json::from_value(Value::Object(result)).map_err(|error| {
                AgentError::tool(format!(
                    "Python model provider returned an invalid response: {error}"
                ))
            })
        })
    }
}

/// Executes trusted Python tools, native tools, or one selected executor route.
pub struct PythonToolExecutor {
    host: SharedPythonProviderHost,
    native: NativeToolExecutor,
    executor: Option<PythonExecutorRoute>,
}

/// Selects one trusted executor driver for capability calls.
#[derive(Clone)]
pub struct PythonExecutorRoute {
    driver: String,
    profile: String,
    policy: Map<String, Value>,
    reuse: ExecutorReuse,
    instance_name: Option<String>,
    bundle: ExecutorBundle,
    lease: Arc<Mutex<Option<String>>>,
}

impl PythonToolExecutor {
    /// Combines one Python host with the native fallback executor.
    pub fn new(host: SharedPythonProviderHost, native: NativeToolExecutor) -> Self {
        Self {
            host,
            native,
            executor: None,
        }
    }

    /// Routes every capability call through one trusted executor driver.
    pub fn with_executor(
        host: SharedPythonProviderHost,
        native: NativeToolExecutor,
        driver: impl Into<String>,
        profile: impl Into<String>,
        policy: Map<String, Value>,
        reuse: ExecutorReuse,
        instance_name: Option<String>,
        bundle: ExecutorBundle,
    ) -> Self {
        Self {
            host,
            native,
            executor: Some(PythonExecutorRoute {
                driver: driver.into(),
                profile: profile.into(),
                policy,
                reuse,
                instance_name,
                bundle,
                lease: Arc::new(Mutex::new(None)),
            }),
        }
    }

    /// Releases one retained executor environment when this agent attempt ends.
    ///
    /// A durable environment remains available for a later name-based attach.
    pub fn finish(&self) -> Result<(), AgentError> {
        let Some(route) = &self.executor else {
            return Ok(());
        };
        if route.reuse == ExecutorReuse::Call {
            return Ok(());
        }
        let handle = route
            .lease
            .lock()
            .map_err(|_error| AgentError::tool("the executor lease is unavailable"))?
            .take();
        let Some(handle) = handle else {
            return Ok(());
        };
        let mut close = Map::new();
        close.insert("handle".to_owned(), Value::String(handle));
        close.insert(
            "disposition".to_owned(),
            Value::String("release".to_owned()),
        );
        self.host
            .lock()
            .map_err(|_error| AgentError::tool("the Python provider host is unavailable"))?
            .close_executor(&route.driver, close, &InactiveAbort)
            .map(|_closed| ())
    }
}

impl CapabilityExecutor for PythonToolExecutor {
    fn execute<'a>(
        &'a mut self,
        invocation: &'a CapabilityInvocation,
        abort: &'a dyn AbortSignal,
    ) -> CapabilityFuture<'a> {
        Box::pin(async move {
            if let Some(route) = &self.executor {
                return self.execute_in_executor(route, invocation, abort);
            }
            let routes_to_python = self
                .host
                .lock()
                .map_err(|_error| AgentError::tool("the Python provider host is unavailable"))?
                .catalog()
                .tools()
                .contains_key(invocation.capability_id.as_str());
            if routes_to_python {
                let mut host = self.host.lock().map_err(|_error| {
                    AgentError::tool("the Python provider host is unavailable")
                })?;
                return host.invoke(
                    invocation.capability_id.as_str(),
                    invocation.params.clone(),
                    invocation.base_directory.clone(),
                    invocation.effect_key.clone(),
                    abort,
                );
            }
            self.native.execute(invocation, abort).await
        })
    }
}

impl PythonToolExecutor {
    fn execute_in_executor(
        &self,
        route: &PythonExecutorRoute,
        invocation: &CapabilityInvocation,
        abort: &dyn AbortSignal,
    ) -> Result<Map<String, Value>, AgentError> {
        match route.reuse {
            ExecutorReuse::Call => self.execute_call_in_executor(route, invocation, abort),
            ExecutorReuse::Session | ExecutorReuse::Durable => {
                self.execute_reused_in_executor(route, invocation, abort)
            }
        }
    }

    fn execute_call_in_executor(
        &self,
        route: &PythonExecutorRoute,
        invocation: &CapabilityInvocation,
        abort: &dyn AbortSignal,
    ) -> Result<Map<String, Value>, AgentError> {
        let handle = self.open_executor(route, invocation, abort)?;
        let result = self.call_executor(route, &handle, invocation, abort);
        let closed = self.close_executor(route, handle, "terminate");
        match (result, closed) {
            (Ok(result), Ok(())) => Ok(result),
            (Ok(_result), Err(error)) => Err(error),
            (Err(error), Ok(()) | Err(_)) => Err(error),
        }
    }

    fn execute_reused_in_executor(
        &self,
        route: &PythonExecutorRoute,
        invocation: &CapabilityInvocation,
        abort: &dyn AbortSignal,
    ) -> Result<Map<String, Value>, AgentError> {
        let handle = {
            let mut lease = route
                .lease
                .lock()
                .map_err(|_error| AgentError::tool("the executor lease is unavailable"))?;
            match lease.as_ref() {
                Some(handle) => handle.clone(),
                None => {
                    let handle = self.open_executor(route, invocation, abort)?;
                    *lease = Some(handle.clone());
                    handle
                }
            }
        };
        self.call_executor(route, &handle, invocation, abort)
    }

    fn open_executor(
        &self,
        route: &PythonExecutorRoute,
        invocation: &CapabilityInvocation,
        abort: &dyn AbortSignal,
    ) -> Result<String, AgentError> {
        route.bundle.verify().map_err(|error| {
            AgentError::tool(format!("the executor bundle is invalid: {error}"))
        })?;
        let mut open = Map::new();
        open.insert("profile".to_owned(), Value::String(route.profile.clone()));
        open.insert("policy".to_owned(), Value::Object(route.policy.clone()));
        let workspace_bundle = serde_json::to_value(route.bundle.workspace()).map_err(|error| {
            AgentError::tool(format!("cannot encode the workspace bundle: {error}"))
        })?;
        open.insert("workspace_bundle".to_owned(), workspace_bundle);
        let tool_bundle = serde_json::to_value(route.bundle.tools())
            .map_err(|error| AgentError::tool(format!("cannot encode the tool bundle: {error}")))?;
        open.insert("tool_bundle".to_owned(), tool_bundle);
        open.insert(
            "reuse".to_owned(),
            Value::String(route.reuse.as_str().to_owned()),
        );
        if route.reuse != ExecutorReuse::Call && route.instance_name.is_none() {
            return Err(AgentError::tool(
                "a reused executor route requires an instance name",
            ));
        }
        if let Some(instance_name) = &route.instance_name {
            open.insert(
                "instance_name".to_owned(),
                Value::String(instance_name.clone()),
            );
        }
        open.insert(
            "capabilities".to_owned(),
            Value::Array(
                route
                    .bundle
                    .tools()
                    .capabilities
                    .iter()
                    .map(|capability| Value::String(capability.id.clone()))
                    .collect(),
            ),
        );
        if let Some(base_directory) = &invocation.base_directory {
            open.insert(
                "base_directory".to_owned(),
                Value::String(base_directory.clone()),
            );
        }
        let mut host = self
            .host
            .lock()
            .map_err(|_error| AgentError::tool("the Python provider host is unavailable"))?;
        let opened = host.open_executor(&route.driver, open, abort)?;
        executor_handle(&opened)
    }

    fn call_executor(
        &self,
        route: &PythonExecutorRoute,
        handle: &str,
        invocation: &CapabilityInvocation,
        abort: &dyn AbortSignal,
    ) -> Result<Map<String, Value>, AgentError> {
        if !route.bundle.permits(invocation.capability_id.as_str()) {
            return Err(AgentError::tool(format!(
                "the executor bundle does not permit capability {:?}",
                invocation.capability_id
            )));
        }
        let mut call = Map::new();
        call.insert("handle".to_owned(), Value::String(handle.to_owned()));
        call.insert(
            "capability".to_owned(),
            Value::String(invocation.capability_id.to_string()),
        );
        call.insert("input".to_owned(), Value::Object(invocation.params.clone()));
        if let Some(effect_key) = &invocation.effect_key {
            call.insert("effect_key".to_owned(), Value::String(effect_key.clone()));
        }
        self.host
            .lock()
            .map_err(|_error| AgentError::tool("the Python provider host is unavailable"))?
            .call_executor(&route.driver, call, abort)
    }

    fn close_executor(
        &self,
        route: &PythonExecutorRoute,
        handle: String,
        disposition: &str,
    ) -> Result<(), AgentError> {
        let mut close = Map::new();
        close.insert("handle".to_owned(), Value::String(handle));
        close.insert(
            "disposition".to_owned(),
            Value::String(disposition.to_owned()),
        );
        self.host
            .lock()
            .map_err(|_error| AgentError::tool("the Python provider host is unavailable"))?
            .close_executor(&route.driver, close, &InactiveAbort)
            .map(|_closed| ())
    }
}

fn executor_handle(value: &Map<String, Value>) -> Result<String, AgentError> {
    value
        .get("handle")
        .and_then(Value::as_str)
        .filter(|handle| !handle.is_empty())
        .map(str::to_owned)
        .ok_or_else(|| AgentError::tool("executor open returned no handle"))
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
        observer: &mut dyn AgentObserver,
        abort: &dyn AbortSignal,
    ) -> Result<Map<String, Value>, AgentError> {
        let mut input = Map::new();
        input.insert("model".to_owned(), Value::String(model.to_owned()));
        input.insert("request".to_owned(), Value::Object(request));
        self.executor.call_with_notifications(
            "generate",
            input,
            None,
            None,
            abort,
            &mut |notification| {
                if notification.method != "model.observation" {
                    return Err(AgentError::tool(format!(
                        "Python model provider sent unsupported notification '{}'",
                        notification.method
                    )));
                }
                let Some(value) = notification.params.get("observation") else {
                    return Err(AgentError::tool(
                        "Python model provider observation has no observation field",
                    ));
                };
                let observation = serde_json::from_value(value.clone()).map_err(|error| {
                    AgentError::tool(format!(
                        "Python model provider sent an invalid observation: {error}"
                    ))
                })?;
                observer.observe(observation);
                Ok(())
            },
        )
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

    /// Opens one executor environment through a trusted Python driver.
    ///
    /// # Errors
    ///
    /// Returns [`AgentError`] when the driver is absent or rejects the request.
    pub fn open_executor(
        &mut self,
        executor: &str,
        request: Map<String, Value>,
        abort: &dyn AbortSignal,
    ) -> Result<Map<String, Value>, AgentError> {
        self.call(
            "executors.open",
            "executor",
            executor,
            request,
            None,
            None,
            abort,
        )
    }

    /// Calls one open executor environment through a trusted Python driver.
    ///
    /// # Errors
    ///
    /// Returns [`AgentError`] when the driver is absent or rejects the request.
    pub fn call_executor(
        &mut self,
        executor: &str,
        request: Map<String, Value>,
        abort: &dyn AbortSignal,
    ) -> Result<Map<String, Value>, AgentError> {
        self.call(
            "executors.call",
            "executor",
            executor,
            request,
            None,
            None,
            abort,
        )
    }

    /// Requests cancellation of one executor call.
    ///
    /// # Errors
    ///
    /// Returns [`AgentError`] when the driver is absent or rejects the request.
    pub fn cancel_executor(
        &mut self,
        executor: &str,
        request: Map<String, Value>,
        abort: &dyn AbortSignal,
    ) -> Result<Map<String, Value>, AgentError> {
        self.call(
            "executors.cancel",
            "executor",
            executor,
            request,
            None,
            None,
            abort,
        )
    }

    /// Closes one executor environment through a trusted Python driver.
    ///
    /// # Errors
    ///
    /// Returns [`AgentError`] when the driver is absent or rejects the request.
    pub fn close_executor(
        &mut self,
        executor: &str,
        request: Map<String, Value>,
        abort: &dyn AbortSignal,
    ) -> Result<Map<String, Value>, AgentError> {
        self.call(
            "executors.close",
            "executor",
            executor,
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
    let executors = parse_category(&mut values, "executors")?;
    if !values.is_empty() {
        return Err(AgentError::tool(
            "Python provider catalog has unknown fields",
        ));
    }
    Ok(PythonProviderCatalog {
        models,
        tools,
        connectors,
        executors,
    })
}

fn serialize_object<T: Serialize>(value: &T, name: &str) -> Result<Value, AgentError> {
    let value = serde_json::to_value(value)
        .map_err(|error| AgentError::tool(format!("cannot serialize {name}: {error}")))?;
    let Value::Object(value) = value else {
        return Err(AgentError::tool(format!(
            "{name} does not encode as an object"
        )));
    };
    Ok(Value::Object(value))
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
