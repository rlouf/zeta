//! Defines authored agent declaration values.

use std::collections::{BTreeMap, BTreeSet};
use std::fmt;
use std::path::PathBuf;
use std::str::FromStr;

use serde::de::Error as _;
use serde::{Deserialize, Deserializer, Serialize, Serializer};
use serde_json::{Map, Value};
use zeta_substrate::{canonical_json, hash_bytes, Hash};

use crate::error::{AuthoringError, AuthoringErrorKind};
use crate::parse::{validate_prompt, SkillResource, SkillSpec};

/// Identifies the exact implementation behind one host-supplied declaration.
///
/// The host decides which bytes define an implementation and supplies their
/// plain content address. Authoring records the value without inspecting a
/// process, package, or filesystem path.
///
/// # Examples
///
/// ```
/// let fingerprint = zeta_authoring::ImplementationFingerprint::new(
///     zeta_substrate::hash_bytes(b"implementation"),
/// );
/// assert_eq!(fingerprint.as_hash(), zeta_substrate::hash_bytes(b"implementation"));
/// ```
#[derive(Clone, Debug, Deserialize, PartialEq, Eq, PartialOrd, Ord, Serialize)]
#[serde(transparent)]
pub struct ImplementationFingerprint(Hash);

impl ImplementationFingerprint {
    /// Creates a fingerprint from a plain content address.
    ///
    /// # Examples
    ///
    /// ```
    /// let hash = zeta_substrate::hash_bytes(b"implementation");
    /// let fingerprint = zeta_authoring::ImplementationFingerprint::new(hash);
    /// assert_eq!(fingerprint.as_hash(), hash);
    /// ```
    pub fn new(hash: Hash) -> Self {
        ImplementationFingerprint(hash)
    }

    /// Returns the plain implementation content address.
    ///
    /// # Examples
    ///
    /// ```
    /// let hash = zeta_substrate::hash_bytes(b"implementation");
    /// let fingerprint = zeta_authoring::ImplementationFingerprint::new(hash);
    /// assert_eq!(fingerprint.as_hash(), hash);
    /// ```
    pub fn as_hash(&self) -> Hash {
        self.0
    }
}

/// Names how an effect may be retried after an interrupted call.
///
/// # Examples
///
/// ```
/// let semantics = zeta_authoring::DeliverySemantics::IdempotentWithKey;
/// assert_eq!(
///     serde_json::to_value(semantics).unwrap(),
///     serde_json::json!("idempotent_with_key")
/// );
/// ```
#[derive(Clone, Copy, Debug, Deserialize, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum DeliverySemantics {
    /// Requires a retry-stable key at the capability boundary.
    IdempotentWithKey,
    /// Delegates deduplication to the external connector.
    ConnectorDeduplicated,
    /// Allows another attempt with the same stable effect identity.
    AtLeastOnce,
    /// Treats an interrupted operation as ambiguous.
    UnsafeToRetry,
}

/// Declares one effectful operation exposed by a connector.
///
/// # Examples
///
/// ```
/// let operation = zeta_authoring::ConnectorOperation {
///     name: "message.post".to_owned(),
///     semantics: zeta_authoring::DeliverySemantics::IdempotentWithKey,
///     options_schema: None,
/// };
/// assert_eq!(operation.name, "message.post");
/// ```
#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ConnectorOperation {
    /// Names the operation and its corresponding event declaration.
    pub name: String,
    /// States the retry contract for the external effect.
    pub semantics: DeliverySemantics,
    /// Validates authored delivery options when present.
    pub options_schema: Option<Map<String, Value>>,
}

/// Holds one language-neutral connector description.
///
/// Launch commands and package metadata are deliberately absent. The host
/// retains them and supplies only the validated declaration and fingerprint.
///
/// # Examples
///
/// ```
/// let value = serde_json::json!({
///     "id": "mail",
///     "protocol_versions": [0],
///     "events": {"mail.received": null}
/// });
/// let connector = zeta_authoring::parse_connector(
///     &value,
///     zeta_authoring::ImplementationFingerprint::new(
///         zeta_substrate::hash_bytes(b"mail connector"),
///     ),
/// )?;
/// assert!(connector.ingress_event("mail.received"));
/// # Ok::<(), zeta_authoring::AuthoringError>(())
/// ```
#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ConnectorSpec {
    /// Identifies the connector independently of its launch command.
    pub id: String,
    /// Lists the supported IPC protocol versions in sorted order.
    pub protocol_versions: Vec<u64>,
    /// Declares every event the connector owns.
    pub events: EventRegistry,
    /// Maps ingress events to authored filter schemas.
    pub filters: BTreeMap<String, Option<Map<String, Value>>>,
    /// Maps egress event names to effect declarations.
    pub operations: BTreeMap<String, ConnectorOperation>,
    /// Lists required host settings in sorted order.
    pub settings: Vec<String>,
    /// Identifies the implementation that supplied the declaration.
    pub implementation: ImplementationFingerprint,
}

impl ConnectorSpec {
    /// Returns whether an event is connector ingress rather than an operation.
    ///
    /// # Examples
    ///
    /// ```
    /// let value = serde_json::json!({
    ///     "id": "mail",
    ///     "protocol_versions": [0],
    ///     "events": {"mail.received": null}
    /// });
    /// let connector = zeta_authoring::parse_connector(
    ///     &value,
    ///     zeta_authoring::ImplementationFingerprint::new(
    ///         zeta_substrate::hash_bytes(b"mail connector"),
    ///     ),
    /// )?;
    /// assert!(connector.ingress_event("mail.received"));
    /// # Ok::<(), zeta_authoring::AuthoringError>(())
    /// ```
    pub fn ingress_event(&self, event_type: &str) -> bool {
        self.events.knows(event_type) && !self.operations.contains_key(event_type)
    }
}

/// Parses one connector description without retaining launch metadata.
///
/// # Errors
///
/// Returns [`AuthoringError`] when the document has an invalid protocol,
/// schema, operation, setting, or identity.
///
/// [`AuthoringError`]: crate::AuthoringError
///
/// # Examples
///
/// ```
/// let value = serde_json::json!({
///     "id": "mail",
///     "protocol_versions": [0],
///     "events": {}
/// });
/// let connector = zeta_authoring::parse_connector(
///     &value,
///     zeta_authoring::ImplementationFingerprint::new(
///         zeta_substrate::hash_bytes(b"mail connector"),
///     ),
/// )?;
/// assert_eq!(connector.id, "mail");
/// # Ok::<(), zeta_authoring::AuthoringError>(())
/// ```
pub fn parse_connector(
    value: &Value,
    implementation: ImplementationFingerprint,
) -> Result<ConnectorSpec, AuthoringError> {
    let Some(value) = value.as_object() else {
        return Err(connector_error(None, "describe output must be an object"));
    };
    let fields = [
        "id",
        "protocol_versions",
        "events",
        "filters",
        "operations",
        "settings",
        "command",
    ];
    for field in value.keys() {
        if !fields.contains(&field.as_str()) {
            return Err(connector_error(
                None,
                format!("describe output has unsupported field {field:?}"),
            ));
        }
    }
    let id = required_connector_string(value, "id", None)?;
    let Some(versions) = value.get("protocol_versions").and_then(Value::as_array) else {
        return Err(connector_error(
            Some(&id),
            "describe output must carry protocol_versions",
        ));
    };
    if versions.is_empty() {
        return Err(connector_error(
            Some(&id),
            "protocol_versions must not be empty",
        ));
    }
    let mut protocol_versions = Vec::new();
    for version in versions {
        let Some(version) = version.as_u64() else {
            return Err(connector_error(
                Some(&id),
                "protocol_versions must contain unsigned integers",
            ));
        };
        if protocol_versions.contains(&version) {
            return Err(connector_error(
                Some(&id),
                "protocol_versions must not contain duplicates",
            ));
        }
        protocol_versions.push(version);
    }
    protocol_versions.sort_unstable();
    if !protocol_versions.contains(&0) {
        return Err(connector_error(
            Some(&id),
            "connector does not support IPC protocol 0",
        ));
    }

    let Some(events) = value.get("events").and_then(Value::as_object) else {
        return Err(connector_error(
            Some(&id),
            "describe events must be an object",
        ));
    };
    let mut event_registry = EventRegistry::new();
    for (event_type, schema) in events {
        let schema = connector_schema(schema, &id, "event", event_type)?;
        if let Err(error) = event_registry.register(event_type, schema) {
            return Err(connector_error(Some(&id), error.to_string()));
        }
    }

    let mut filters = BTreeMap::new();
    let filters_value = value
        .get("filters")
        .cloned()
        .unwrap_or_else(|| Value::Object(Map::new()));
    let Some(filter_values) = filters_value.as_object() else {
        return Err(connector_error(
            Some(&id),
            "describe filters must be an object",
        ));
    };
    for (event_type, schema) in filter_values {
        if !event_registry.knows(event_type) {
            return Err(connector_error(
                Some(&id),
                format!("filter {event_type:?} has no event declaration"),
            ));
        }
        let schema = connector_schema(schema, &id, "filter", event_type)?;
        filters.insert(event_type.clone(), schema);
    }

    let mut operations = BTreeMap::new();
    let operations_value = value
        .get("operations")
        .cloned()
        .unwrap_or_else(|| Value::Array(Vec::new()));
    let Some(operation_values) = operations_value.as_array() else {
        return Err(connector_error(
            Some(&id),
            "describe operations must be an array",
        ));
    };
    for operation in operation_values {
        let Some(operation) = operation.as_object() else {
            return Err(connector_error(
                Some(&id),
                "describe operation must be an object",
            ));
        };
        let name = required_connector_string(operation, "name", Some(&id))?;
        if !event_registry.knows(&name) {
            return Err(connector_error(
                Some(&id),
                format!("operation {name:?} has no event declaration"),
            ));
        }
        if operations.contains_key(&name) {
            return Err(connector_error(
                Some(&id),
                format!("operation {name:?} is duplicated"),
            ));
        }
        let semantics = required_connector_string(operation, "semantics", Some(&id))?;
        let Some(semantics) = delivery_semantics(&semantics) else {
            return Err(connector_error(
                Some(&id),
                format!("operation {name:?} has invalid delivery semantics"),
            ));
        };
        let options_schema = match operation.get("options_schema") {
            Some(schema) => connector_schema(schema, &id, "operation", &name)?,
            None => None,
        };
        operations.insert(
            name.clone(),
            ConnectorOperation {
                name,
                semantics,
                options_schema,
            },
        );
    }

    let settings_value = value
        .get("settings")
        .cloned()
        .unwrap_or_else(|| Value::Array(Vec::new()));
    let Some(setting_values) = settings_value.as_array() else {
        return Err(connector_error(
            Some(&id),
            "describe settings must be an array",
        ));
    };
    let mut settings = Vec::new();
    for setting in setting_values {
        let Some(setting) = setting.as_str() else {
            return Err(connector_error(
                Some(&id),
                "describe settings must contain strings",
            ));
        };
        if setting.is_empty() {
            return Err(connector_error(
                Some(&id),
                "describe settings must contain non-empty strings",
            ));
        }
        if settings.contains(&setting.to_owned()) {
            return Err(connector_error(
                Some(&id),
                format!("describe setting {setting:?} is duplicated"),
            ));
        }
        settings.push(setting.to_owned());
    }
    settings.sort();

    Ok(ConnectorSpec {
        id,
        protocol_versions,
        events: event_registry,
        filters,
        operations,
        settings,
        implementation,
    })
}

fn required_connector_string(
    values: &Map<String, Value>,
    field: &str,
    connector_id: Option<&str>,
) -> Result<String, AuthoringError> {
    let Some(value) = values.get(field).and_then(Value::as_str) else {
        return Err(connector_error(
            connector_id,
            format!("{field} must be a non-empty string"),
        ));
    };
    if value.is_empty() {
        return Err(connector_error(
            connector_id,
            format!("{field} must be a non-empty string"),
        ));
    }
    Ok(value.to_owned())
}

fn connector_schema(
    value: &Value,
    connector_id: &str,
    kind: &str,
    name: &str,
) -> Result<Option<Map<String, Value>>, AuthoringError> {
    if value.is_null() {
        return Ok(None);
    }
    let Some(schema) = value.as_object() else {
        return Err(connector_error(
            Some(connector_id),
            format!("{kind} schema for {name:?} must be an object"),
        ));
    };
    if let Err(error) = validate_schema(name, schema) {
        return Err(connector_error(Some(connector_id), error.to_string()));
    }
    Ok(Some(schema.clone()))
}

fn delivery_semantics(value: &str) -> Option<DeliverySemantics> {
    if value == "idempotent_with_key" {
        return Some(DeliverySemantics::IdempotentWithKey);
    }
    if value == "connector_deduplicated" {
        return Some(DeliverySemantics::ConnectorDeduplicated);
    }
    if value == "at_least_once" {
        return Some(DeliverySemantics::AtLeastOnce);
    }
    if value == "unsafe_to_retry" {
        return Some(DeliverySemantics::UnsafeToRetry);
    }
    None
}

fn connector_error(connector_id: Option<&str>, detail: impl Into<String>) -> AuthoringError {
    AuthoringError::new(
        AuthoringErrorKind::InvalidConnector,
        connector_id,
        None,
        detail,
    )
}

/// Holds the validated event vocabulary for one authored project.
///
/// Entries iterate in event-name order. A known event may omit its payload
/// schema, which remains distinct from an unknown event.
///
/// # Examples
///
/// ```
/// let mut events = zeta_authoring::EventRegistry::new();
/// events.register("work.requested", None)?;
/// assert!(events.knows("work.requested"));
/// # Ok::<(), zeta_authoring::AuthoringError>(())
/// ```
#[derive(Clone, Debug, Default, Deserialize, PartialEq, Serialize)]
#[serde(transparent)]
pub struct EventRegistry {
    events: BTreeMap<String, Option<Map<String, Value>>>,
}

impl EventRegistry {
    /// Creates an empty event vocabulary.
    ///
    /// # Examples
    ///
    /// ```
    /// let events = zeta_authoring::EventRegistry::new();
    /// assert_eq!(events.iter().count(), 0);
    /// ```
    pub fn new() -> Self {
        EventRegistry {
            events: BTreeMap::new(),
        }
    }

    /// Registers one event and validates its optional Draft 2020-12 schema.
    ///
    /// Registering the same event with the same schema is idempotent. A
    /// different declaration for an existing event is a conflict.
    ///
    /// # Errors
    ///
    /// Returns [`AuthoringError`] when the event is empty, its schema is
    /// malformed, or it conflicts with an existing declaration.
    ///
    /// [`AuthoringError`]: crate::AuthoringError
    ///
    /// # Examples
    ///
    /// ```
    /// let mut events = zeta_authoring::EventRegistry::new();
    /// events.register("work.requested", None)?;
    /// events.register("work.requested", None)?;
    /// assert_eq!(events.iter().count(), 1);
    /// # Ok::<(), zeta_authoring::AuthoringError>(())
    /// ```
    pub fn register(
        &mut self,
        event_type: &str,
        schema: Option<Map<String, Value>>,
    ) -> Result<(), AuthoringError> {
        if event_type.is_empty() {
            return Err(AuthoringError::new(
                AuthoringErrorKind::InvalidSchema,
                Some(event_type),
                None,
                "event type must be non-empty",
            ));
        }
        if let Some(schema) = &schema {
            validate_schema(event_type, schema)?;
        }
        if let Some(existing) = self.events.get(event_type) {
            if existing == &schema {
                return Ok(());
            }
            return Err(AuthoringError::new(
                AuthoringErrorKind::ConflictingDeclaration,
                Some(event_type),
                None,
                "event is already registered with a different schema",
            ));
        }
        self.events.insert(event_type.to_owned(), schema);
        Ok(())
    }

    /// Registers the empty payload schema for one agent schedule.
    ///
    /// # Errors
    ///
    /// Returns [`AuthoringError`] if the synthetic event conflicts with an
    /// existing event declaration.
    ///
    /// [`AuthoringError`]: crate::AuthoringError
    ///
    /// # Examples
    ///
    /// ```
    /// let mut events = zeta_authoring::EventRegistry::new();
    /// events.register_scheduled("digest")?;
    /// assert!(events.knows("agent.digest.scheduled"));
    /// # Ok::<(), zeta_authoring::AuthoringError>(())
    /// ```
    pub fn register_scheduled(&mut self, agent_slug: &str) -> Result<(), AuthoringError> {
        self.register(
            &scheduled_event_type(agent_slug),
            Some(empty_payload_schema()),
        )
    }

    /// Returns whether an event is present in the vocabulary.
    ///
    /// # Examples
    ///
    /// ```
    /// let mut events = zeta_authoring::EventRegistry::new();
    /// events.register("work.requested", None)?;
    /// assert!(events.knows("work.requested"));
    /// # Ok::<(), zeta_authoring::AuthoringError>(())
    /// ```
    pub fn knows(&self, event_type: &str) -> bool {
        self.events.contains_key(event_type)
    }

    /// Returns a known event's optional schema.
    ///
    /// The outer [`Option<T>`] distinguishes an unknown event. The inner
    /// [`Option<T>`] distinguishes a known schema-less event.
    ///
    /// # Examples
    ///
    /// ```
    /// let mut events = zeta_authoring::EventRegistry::new();
    /// events.register("work.requested", None)?;
    /// assert_eq!(events.schema("work.requested"), Some(None));
    /// assert_eq!(events.schema("missing"), None);
    /// # Ok::<(), zeta_authoring::AuthoringError>(())
    /// ```
    pub fn schema(&self, event_type: &str) -> Option<Option<&Map<String, Value>>> {
        self.events.get(event_type).map(Option::as_ref)
    }

    /// Iterates over events in deterministic name order.
    ///
    /// # Examples
    ///
    /// ```
    /// let mut events = zeta_authoring::EventRegistry::new();
    /// events.register("z.last", None)?;
    /// events.register("a.first", None)?;
    /// assert_eq!(events.iter().next().unwrap().0, "a.first");
    /// # Ok::<(), zeta_authoring::AuthoringError>(())
    /// ```
    pub fn iter(&self) -> impl Iterator<Item = (&String, &Option<Map<String, Value>>)> {
        self.events.iter()
    }
}

/// Derives the structured result schema for one authored agent.
///
/// Each returned event becomes one discriminated `type` and `payload` branch.
/// Branch-local definitions are renamed and hoisted so independently authored
/// schemas cannot collide.
///
/// # Errors
///
/// Returns [`AuthoringError`] when a returned event is not in `events`.
///
/// [`AuthoringError`]: crate::AuthoringError
///
/// # Examples
///
/// ```
/// let spec = zeta_authoring::parse_agent(
///     "worker",
///     b"---\nname: Worker\ndescription: Works.\nreturns: [work.completed]\n---\n",
/// )?;
/// let mut events = zeta_authoring::EventRegistry::new();
/// events.register("work.completed", None)?;
/// let schema = zeta_authoring::derive_returns_schema(&spec, &events)?.unwrap();
/// assert_eq!(schema["oneOf"][0]["properties"]["type"]["const"], "work.completed");
/// # Ok::<(), Box<dyn std::error::Error>>(())
/// ```
pub fn derive_returns_schema(
    spec: &AgentSpec,
    events: &EventRegistry,
) -> Result<Option<Value>, AuthoringError> {
    if spec.returns.is_empty() {
        return Ok(None);
    }
    let mut branches = Vec::new();
    let mut definitions = Map::new();
    for (branch_index, event_type) in spec.returns.iter().enumerate() {
        let Some(schema) = events.schema(event_type) else {
            return Err(AuthoringError::new(
                AuthoringErrorKind::UnknownEvent,
                Some(event_type),
                Some("returns"),
                format!(
                    "agent {:?} references unknown event {event_type:?}",
                    spec.slug
                ),
            ));
        };
        let schema = match schema {
            Some(schema) => schema.clone(),
            None => Map::new(),
        };
        let (payload, local_definitions) = hoist_local_definitions(schema, branch_index);
        for (name, definition) in local_definitions {
            definitions.insert(name, definition);
        }
        branches.push(Value::Object(Map::from_iter([
            ("type".to_owned(), Value::String("object".to_owned())),
            (
                "required".to_owned(),
                Value::Array(vec![
                    Value::String("type".to_owned()),
                    Value::String("payload".to_owned()),
                ]),
            ),
            (
                "properties".to_owned(),
                Value::Object(Map::from_iter([
                    (
                        "type".to_owned(),
                        Value::Object(Map::from_iter([(
                            "const".to_owned(),
                            Value::String(event_type.clone()),
                        )])),
                    ),
                    ("payload".to_owned(), Value::Object(payload)),
                ])),
            ),
            ("additionalProperties".to_owned(), Value::Bool(false)),
        ])));
    }
    let mut schema = Map::from_iter([
        ("type".to_owned(), Value::String("object".to_owned())),
        ("oneOf".to_owned(), Value::Array(branches)),
    ]);
    if !definitions.is_empty() {
        schema.insert("$defs".to_owned(), Value::Object(definitions));
    }
    Ok(Some(Value::Object(schema)))
}

fn validate_schema(event_type: &str, schema: &Map<String, Value>) -> Result<(), AuthoringError> {
    let schema = Value::Object(schema.clone());
    if jsonschema::draft202012::meta::is_valid(&schema) {
        return Ok(());
    }
    Err(AuthoringError::new(
        AuthoringErrorKind::InvalidSchema,
        Some(event_type),
        None,
        "event payload schema is not a valid Draft 2020-12 schema",
    ))
}

fn empty_payload_schema() -> Map<String, Value> {
    Map::from_iter([
        ("type".to_owned(), Value::String("object".to_owned())),
        ("additionalProperties".to_owned(), Value::Bool(false)),
    ])
}

fn hoist_local_definitions(
    mut schema: Map<String, Value>,
    branch_index: usize,
) -> (Map<String, Value>, Map<String, Value>) {
    let definitions = schema.remove("$defs");
    let Some(Value::Object(definitions)) = definitions else {
        return (schema, Map::new());
    };
    let mut renamed = BTreeMap::new();
    for name in definitions.keys() {
        renamed.insert(name.clone(), format!("event_{branch_index}_{name}"));
    }
    let mut payload = Value::Object(schema);
    rewrite_local_references(&mut payload, &renamed);
    let Value::Object(payload) = payload else {
        unreachable!("payload is constructed as an object");
    };
    let mut hoisted = Map::new();
    for (name, mut definition) in definitions {
        rewrite_local_references(&mut definition, &renamed);
        let renamed_name = renamed
            .get(&name)
            .expect("every local definition has a renamed identity")
            .clone();
        hoisted.insert(renamed_name, definition);
    }
    (payload, hoisted)
}

fn rewrite_local_references(value: &mut Value, renamed: &BTreeMap<String, String>) {
    match value {
        Value::Object(values) => {
            if let Some(Value::String(reference)) = values.get_mut("$ref") {
                if let Some(name) = reference.strip_prefix("#/$defs/") {
                    if let Some(name) = renamed.get(name) {
                        *reference = format!("#/$defs/{name}");
                    }
                }
            }
            for value in values.values_mut() {
                rewrite_local_references(value, renamed);
            }
        }
        Value::Array(values) => {
            for value in values {
                rewrite_local_references(value, renamed);
            }
        }
        Value::Null | Value::Bool(_) | Value::Number(_) | Value::String(_) => {}
    }
}

/// Declares one schedule attached to an agent.
///
/// # Examples
///
/// ```
/// let schedule = zeta_authoring::ScheduleEntry {
///     cron: "0 18 * * 0".to_owned(),
///     timezone: Some("Europe/Paris".to_owned()),
///     catchup: Some("latest".to_owned()),
/// };
/// assert_eq!(schedule.timezone.as_deref(), Some("Europe/Paris"));
/// ```
#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ScheduleEntry {
    /// Carries the cron expression.
    pub cron: String,
    /// Names the schedule timezone when one is authored.
    pub timezone: Option<String>,
    /// Names the requested catch-up rule.
    pub catchup: Option<String>,
}

/// Selects one concrete model endpoint.
///
/// # Examples
///
/// ```
/// let model = zeta_authoring::ModelSpec {
///     name: "qwen3.6".to_owned(),
///     url: "http://127.0.0.1:8080/v1/chat/completions".to_owned(),
/// };
/// assert_eq!(model.name, "qwen3.6");
/// ```
#[derive(Clone, Debug, Deserialize, PartialEq, Eq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ModelSpec {
    /// Names the model served by the endpoint.
    pub name: String,
    /// Carries the model endpoint URL.
    pub url: String,
}

/// Overrides the retry policy for one agent.
///
/// # Examples
///
/// ```
/// let retry = zeta_authoring::RetrySpec {
///     max_attempts: Some(3),
///     backoff_seconds: Some(1.5),
/// };
/// assert_eq!(retry.max_attempts, Some(3));
/// ```
#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct RetrySpec {
    /// Bounds the total number of attempts when present.
    pub max_attempts: Option<u64>,
    /// Carries the delay before another attempt when present.
    pub backoff_seconds: Option<f64>,
}

/// Selects a tool executor and its JSON configuration.
///
/// # Examples
///
/// ```
/// let executor = zeta_authoring::ExecutorSpec::default();
/// assert_eq!(executor.provider, "local");
/// assert!(executor.config.is_empty());
/// ```
#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ExecutorSpec {
    /// Names the executor provider.
    pub provider: String,
    /// Carries provider-specific JSON configuration.
    pub config: Map<String, Value>,
}

impl Default for ExecutorSpec {
    fn default() -> Self {
        ExecutorSpec {
            provider: "local".to_owned(),
            config: Map::new(),
        }
    }
}

/// Declares how an external event enters an agent.
///
/// # Examples
///
/// ```
/// let binding = zeta_authoring::IngressBinding {
///     event: "message.received".to_owned(),
///     filter: serde_json::Map::new(),
///     idempotency_key: Some("message:{id}".to_owned()),
/// };
/// assert_eq!(binding.event, "message.received");
/// ```
#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct IngressBinding {
    /// Names the accepted event.
    pub event: String,
    /// Carries connector-specific filter values.
    pub filter: Map<String, Value>,
    /// Defines the stable external idempotency identity.
    pub idempotency_key: Option<String>,
}

/// Declares how an agent publishes an external event.
///
/// # Examples
///
/// ```
/// let binding = zeta_authoring::EgressBinding {
///     event: "message.send".to_owned(),
///     options: serde_json::Map::new(),
///     idempotency_key: None,
/// };
/// assert_eq!(binding.event, "message.send");
/// ```
#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct EgressBinding {
    /// Names the published event.
    pub event: String,
    /// Carries connector-specific delivery options.
    pub options: Map<String, Value>,
    /// Defines a caller-authored idempotency identity when present.
    pub idempotency_key: Option<String>,
}

/// Holds one validated authored agent declaration.
///
/// The content address identifies the exact source bytes independently of
/// where they were loaded.
///
/// # Examples
///
/// ```
/// let spec = zeta_authoring::parse_agent(
///     "worker",
///     b"---\nname: Worker\ndescription: Does work.\n---\nWork.\n",
/// )?;
/// assert_eq!(spec.slug, "worker");
/// # Ok::<(), zeta_authoring::SpecError>(())
/// ```
#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct AgentSpec {
    /// Carries the validated lowercase agent identifier.
    pub slug: String,
    /// Carries the authored display name.
    pub name: String,
    /// Carries the authored description.
    pub description: String,
    /// Preserves the Markdown body after frontmatter.
    pub instructions: String,
    /// Preserves the exact validated UTF-8 source for identity verification.
    pub source: String,
    /// Identifies the exact source bytes with plain BLAKE3.
    pub content_address: Hash,
    /// Controls whether the agent may receive events.
    pub enabled: bool,
    /// Carries `shared`, `per-event`, or an authored session template.
    pub session: String,
    /// Selects a concrete model endpoint when authored.
    pub model: Option<ModelSpec>,
    /// Selects the tool executor.
    pub executor: ExecutorSpec,
    /// Lists event types the agent accepts.
    pub accepts: Vec<String>,
    /// Lists event types the agent may publish.
    pub publishes: Vec<String>,
    /// Lists event types the agent may return.
    pub returns: Vec<String>,
    /// Lists explicitly selected skills.
    pub skills: Vec<String>,
    /// Records whether skills were omitted and should be inherited.
    pub skills_inherit: bool,
    /// Lists explicitly selected tools.
    pub tools: Vec<String>,
    /// Records whether tools were omitted and should be inherited.
    pub tools_inherit: bool,
    /// Carries structural schedules.
    pub schedules: Vec<ScheduleEntry>,
    /// Overrides retry policy when authored.
    pub retry: Option<RetrySpec>,
    /// Carries the authored base directory.
    pub base_dir: Option<PathBuf>,
    /// Carries typed ingress bindings parsed from `accepts`.
    pub ingress: Vec<IngressBinding>,
    /// Carries typed egress bindings parsed from `publishes`.
    pub egress: Vec<EgressBinding>,
    /// Lists runtime lock identities required by one invocation.
    #[serde(default)]
    pub locks: Vec<String>,
    /// Preserves non-core frontmatter for later validation.
    pub extensions: Map<String, Value>,
}

const RESERVED_TOOL_NAMES: [&str; 6] = [
    "publish_event",
    "zeta.publish_event",
    "wait_for",
    "zeta.wait_for",
    "cancel",
    "zeta.cancel",
];

/// Identifies a provider-qualified capability declaration.
///
/// The identity contains at least one provider segment and one operation
/// segment. It carries no executable callback or language-specific import.
#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord, Serialize)]
#[serde(transparent)]
pub struct CapabilityId(String);

impl CapabilityId {
    /// Returns the canonical provider-qualified identity.
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl fmt::Display for CapabilityId {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        self.0.fmt(formatter)
    }
}

impl FromStr for CapabilityId {
    type Err = AuthoringError;

    fn from_str(value: &str) -> Result<Self, Self::Err> {
        let mut segments = value.split('.');
        let provider = segments.next();
        let operation = segments.next();
        let mut invalid_segment = false;
        for segment in value.split('.') {
            if segment.is_empty() {
                invalid_segment = true;
            }
        }
        let mut contains_whitespace = false;
        for character in value.chars() {
            if character.is_whitespace() {
                contains_whitespace = true;
            }
        }
        if provider.is_none() || operation.is_none() || invalid_segment || contains_whitespace {
            return Err(AuthoringError::new(
                AuthoringErrorKind::InvalidCapability,
                Some(value),
                Some("id"),
                "capability id must contain non-empty provider and operation segments",
            ));
        }
        Ok(CapabilityId(value.to_owned()))
    }
}

impl<'de> Deserialize<'de> for CapabilityId {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        let value = String::deserialize(deserializer)?;
        value
            .parse::<CapabilityId>()
            .map_err(|error| D::Error::custom(error.to_string()))
    }
}

/// Declares one host-supplied capability without an executable callback.
#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct CapabilitySpec {
    /// Carries the canonical provider-qualified identity.
    pub id: CapabilityId,
    /// Carries the model-facing name used by authored tool selections.
    pub name: String,
    /// Explains the operation to a model.
    pub description: String,
    /// Validates canonical JSON arguments.
    pub input_schema: Map<String, Value>,
    /// States the retry contract for an effectful operation.
    pub delivery_semantics: Option<DeliverySemantics>,
    /// Restricts an authored capability to one agent when present.
    pub owner: Option<String>,
    /// Identifies the implementation supplied by the host.
    pub implementation: ImplementationFingerprint,
}

/// Declares one host-supplied executor provider.
#[derive(Clone, Debug, Deserialize, PartialEq, Eq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ExecutorProviderSpec {
    /// Names the provider selected from agent frontmatter.
    pub id: String,
    /// Identifies the provider implementation supplied by the host.
    pub implementation: ImplementationFingerprint,
}

/// Declares one host-resolved model selection.
#[derive(Clone, Debug, Deserialize, PartialEq, Eq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ModelSelectionSpec {
    /// Names the configuration profile.
    pub profile: String,
    /// Names the provider model.
    pub model: String,
    /// Carries the provider endpoint.
    pub url: String,
    /// Carries an optional reasoning selection.
    pub thinking: Option<String>,
    /// Selects the provider protocol.
    pub api: String,
    /// Selects the model-facing tool presentation.
    pub tool_profile: String,
    /// Identifies the model adapter implementation.
    pub implementation: ImplementationFingerprint,
}

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
/// Returns [`AuthoringError`] for conflicting declarations, invalid schemas,
/// unknown references, invalid bindings, or unsupported extensions.
pub fn compile_project(input: AgentProjectInput) -> Result<AgentProject, AuthoringError> {
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
                return Err(AuthoringError::new(
                    AuthoringErrorKind::DuplicateDeclaration,
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
/// Returns [`AuthoringError`] when the prompt, references, extensions, or
/// connector bindings are invalid.
pub fn validate_agent(
    spec: &AgentSpec,
    context: &AgentValidationContext<'_>,
) -> Result<(), AuthoringError> {
    validate_prompt(spec)?;
    if let Some((extension, _value)) = spec.extensions.iter().next() {
        return Err(AuthoringError::new(
            AuthoringErrorKind::UnknownExtension,
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
                return Err(AuthoringError::new(
                    AuthoringErrorKind::ReservedTool,
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
                return Err(AuthoringError::new(
                    AuthoringErrorKind::UnknownSkill,
                    Some(&spec.slug),
                    Some("skills"),
                    format!("lists unknown skill {name:?}"),
                ));
            }
        }
    }
    if let Some(providers) = context.executor_providers {
        if !providers.contains_key(&spec.executor.provider) {
            return Err(AuthoringError::new(
                AuthoringErrorKind::UnknownExecutorProvider,
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

fn duplicate_declaration(kind: &str, id: &str) -> AuthoringError {
    AuthoringError::new(
        AuthoringErrorKind::DuplicateDeclaration,
        Some(id),
        None,
        format!("duplicate {kind} declaration"),
    )
}

fn validate_skill_resource(skill: &SkillResource) -> Result<(), AuthoringError> {
    if skill.name.is_empty() {
        return Err(AuthoringError::new(
            AuthoringErrorKind::InvalidSkill,
            Some(&skill.name),
            Some("name"),
            "skill name must be non-empty",
        ));
    }
    let expected = SkillResource::new(&skill.name, skill.body.as_bytes())?;
    if expected.object_id != skill.object_id {
        return Err(AuthoringError::new(
            AuthoringErrorKind::InvalidIdentity,
            Some(&skill.name),
            Some("object_id"),
            "flat skill content address does not match its body",
        ));
    }
    Ok(())
}

fn validate_skill_spec(skill: &SkillSpec) -> Result<(), AuthoringError> {
    if !valid_skill_declaration_name(&skill.name) || skill.description.trim().is_empty() {
        return Err(AuthoringError::new(
            AuthoringErrorKind::InvalidSkill,
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
) -> Result<(), AuthoringError> {
    capability.id.as_str().parse::<CapabilityId>()?;
    if reserved_tool(capability.id.as_str()) || reserved_tool(&capability.name) {
        return Err(AuthoringError::new(
            AuthoringErrorKind::ReservedTool,
            Some(capability.id.as_str()),
            Some("name"),
            format!(
                "capability name {:?} is reserved by the runtime",
                capability.name
            ),
        ));
    }
    if capability.name.is_empty() || capability.description.trim().is_empty() {
        return Err(AuthoringError::new(
            AuthoringErrorKind::InvalidCapability,
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
        return Err(AuthoringError::new(
            AuthoringErrorKind::UnknownAgent,
            Some(owner),
            Some("owner"),
            format!("capability {:?} has an unknown owner", capability.id),
        ));
    }
    let prefix = format!("agent.{owner}.");
    if !capability.id.as_str().starts_with(&prefix) {
        return Err(AuthoringError::new(
            AuthoringErrorKind::InvalidCapability,
            Some(capability.id.as_str()),
            Some("owner"),
            format!("agent-owned capability must start with {prefix:?}"),
        ));
    }
    Ok(())
}

fn validate_connector_spec(connector: &ConnectorSpec) -> Result<(), AuthoringError> {
    if connector.id.is_empty() {
        return Err(connector_error(None, "connector id must be non-empty"));
    }
    if connector.protocol_versions.is_empty() || !connector.protocol_versions.contains(&0) {
        return Err(connector_error(
            Some(&connector.id),
            "connector must support IPC protocol 0",
        ));
    }
    let mut versions = BTreeSet::new();
    for version in &connector.protocol_versions {
        if !versions.insert(*version) {
            return Err(connector_error(
                Some(&connector.id),
                "protocol_versions must not contain duplicates",
            ));
        }
    }
    let mut events = EventRegistry::new();
    for (event_type, schema) in connector.events.iter() {
        events
            .register(event_type, schema.clone())
            .map_err(|error| connector_error(Some(&connector.id), error.to_string()))?;
    }
    for (event_type, schema) in &connector.filters {
        if !events.knows(event_type) {
            return Err(connector_error(
                Some(&connector.id),
                format!("filter {event_type:?} has no event declaration"),
            ));
        }
        if let Some(schema) = schema {
            validate_schema(event_type, schema)
                .map_err(|error| connector_error(Some(&connector.id), error.to_string()))?;
        }
    }
    for (event_type, operation) in &connector.operations {
        if event_type != &operation.name || !events.knows(event_type) {
            return Err(connector_error(
                Some(&connector.id),
                format!("operation {event_type:?} does not match an event declaration"),
            ));
        }
        if let Some(schema) = &operation.options_schema {
            validate_schema(event_type, schema)
                .map_err(|error| connector_error(Some(&connector.id), error.to_string()))?;
        }
    }
    let mut settings = BTreeSet::new();
    for setting in &connector.settings {
        if setting.is_empty() || !settings.insert(setting) {
            return Err(connector_error(
                Some(&connector.id),
                "settings must be unique non-empty strings",
            ));
        }
    }
    Ok(())
}

fn validate_agent_declaration(spec: &AgentSpec) -> Result<(), AuthoringError> {
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
        if !base_dir.starts_with('/') && base_dir != "~" && !base_dir.starts_with("~/") {
            return Err(invalid_agent(
                spec,
                Some("base_dir"),
                "base directory must be absolute or home-relative",
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

fn verify_authored_agent(spec: &AgentSpec) -> Result<(), AuthoringError> {
    if hash_bytes(spec.source.as_bytes()) != spec.content_address {
        return Err(AuthoringError::new(
            AuthoringErrorKind::InvalidIdentity,
            Some(&spec.slug),
            Some("content_address"),
            "agent source content address does not match its exact source",
        ));
    }
    let parsed =
        crate::parse::parse_agent(&spec.slug, spec.source.as_bytes()).map_err(|error| {
            AuthoringError::new(
                AuthoringErrorKind::InvalidAgent,
                Some(&spec.slug),
                Some("source"),
                error.to_string(),
            )
        })?;
    if &parsed != spec {
        return Err(AuthoringError::new(
            AuthoringErrorKind::InvalidAgent,
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
) -> AuthoringError {
    AuthoringError::new(
        AuthoringErrorKind::InvalidAgent,
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

fn validate_executor_provider(provider: &ExecutorProviderSpec) -> Result<(), AuthoringError> {
    let mut contains_whitespace = false;
    for character in provider.id.chars() {
        if character.is_whitespace() {
            contains_whitespace = true;
        }
    }
    if provider.id.is_empty() || contains_whitespace {
        return Err(AuthoringError::new(
            AuthoringErrorKind::InvalidExecutorProvider,
            Some(&provider.id),
            Some("id"),
            "executor provider id must be non-empty and contain no whitespace",
        ));
    }
    Ok(())
}

fn validate_model_selection(model: &ModelSelectionSpec) -> Result<(), AuthoringError> {
    let fields = [
        ("profile", model.profile.as_str()),
        ("model", model.model.as_str()),
        ("url", model.url.as_str()),
        ("api", model.api.as_str()),
        ("tool_profile", model.tool_profile.as_str()),
    ];
    for (field, value) in fields {
        if value.trim().is_empty() {
            return Err(AuthoringError::new(
                AuthoringErrorKind::InvalidModel,
                Some(&model.profile),
                Some(field),
                format!("model selection {field} must be non-empty"),
            ));
        }
    }
    Ok(())
}

fn normalize_agent_skills(
    spec: &mut AgentSpec,
    skill_names: &BTreeSet<String>,
) -> Result<(), AuthoringError> {
    if spec.skills_inherit {
        spec.skills = skill_names.iter().cloned().collect();
        spec.skills_inherit = false;
        return Ok(());
    }
    for name in &spec.skills {
        if !skill_names.contains(name) {
            return Err(AuthoringError::new(
                AuthoringErrorKind::UnknownSkill,
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
) -> Result<(), AuthoringError> {
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
                return Err(AuthoringError::new(
                    AuthoringErrorKind::ReservedTool,
                    Some(&spec.slug),
                    Some("tools"),
                    format!("lists reserved tool {name:?}"),
                ));
            }
            let capability = selected_capability(spec, name, capabilities)?;
            if reserved_tool(capability.id.as_str()) {
                return Err(AuthoringError::new(
                    AuthoringErrorKind::ReservedTool,
                    Some(&spec.slug),
                    Some("tools"),
                    format!("lists reserved tool {name:?}"),
                ));
            }
            if let Some(owner) = &capability.owner {
                if owner != &spec.slug {
                    return Err(AuthoringError::new(
                        AuthoringErrorKind::UnknownTool,
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
) -> Result<&'a CapabilitySpec, AuthoringError> {
    if let Some(capability) = capabilities.get(name) {
        return Ok(capability);
    }
    let mut resolved = None;
    for capability in capabilities.values() {
        if capability.name != name {
            continue;
        }
        if resolved.is_some() {
            return Err(AuthoringError::new(
                AuthoringErrorKind::ConflictingDeclaration,
                Some(&spec.slug),
                Some("tools"),
                format!("tool alias {name:?} matches multiple capabilities"),
            ));
        }
        resolved = Some(capability);
    }
    let Some(resolved) = resolved else {
        return Err(AuthoringError::new(
            AuthoringErrorKind::UnknownTool,
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
) -> Result<(), AuthoringError> {
    if reserved_tool(name) {
        return Err(AuthoringError::new(
            AuthoringErrorKind::ReservedTool,
            Some(&spec.slug),
            Some("tools"),
            format!("lists reserved tool {name:?}"),
        ));
    }
    let capability = selected_capability(spec, name, capabilities)?;
    if let Some(owner) = &capability.owner {
        if owner != &spec.slug {
            return Err(AuthoringError::new(
                AuthoringErrorKind::UnknownTool,
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
) -> Result<(), AuthoringError> {
    let references = [
        ("accepts", spec.accepts.as_slice()),
        ("publishes", spec.publishes.as_slice()),
        ("returns", spec.returns.as_slice()),
    ];
    for (field, event_types) in references {
        for event_type in event_types {
            if !events.knows(event_type) {
                return Err(AuthoringError::new(
                    AuthoringErrorKind::UnknownEvent,
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
) -> Result<(), AuthoringError> {
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
) -> Result<(), AuthoringError> {
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
) -> AuthoringError {
    AuthoringError::new(
        AuthoringErrorKind::InvalidBinding,
        Some(&spec.slug),
        Some(field),
        detail,
    )
}

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

/// Returns whether an enabled agent accepts an exact event type.
///
/// # Examples
///
/// ```
/// let spec = zeta_authoring::parse_agent(
///     "worker",
///     b"---\nname: Worker\ndescription: Does work.\naccepts: [work.requested]\n---\n",
/// )?;
/// assert!(zeta_authoring::matches(&spec, "work.requested"));
/// # Ok::<(), zeta_authoring::SpecError>(())
/// ```
pub fn matches(spec: &AgentSpec, event_type: &str) -> bool {
    if !spec.enabled {
        return false;
    }
    for accepted in &spec.accepts {
        if accepted == event_type {
            return true;
        }
    }
    false
}

/// Returns the synthetic event type used by an agent schedule.
///
/// # Examples
///
/// ```
/// assert_eq!(
///     zeta_authoring::scheduled_event_type("digest"),
///     "agent.digest.scheduled"
/// );
/// ```
pub fn scheduled_event_type(agent_slug: &str) -> String {
    format!("agent.{agent_slug}.scheduled")
}
