//! Defines connector declarations and parsing.

use std::collections::{BTreeMap, BTreeSet};

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};

use crate::error::{AuthoringError, AuthoringErrorKind};
use crate::event::{validate_schema, EventRegistry};
use crate::spec::{DeliverySemantics, ImplementationFingerprint};

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

pub(crate) fn validate_connector_spec(connector: &ConnectorSpec) -> Result<(), AuthoringError> {
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
