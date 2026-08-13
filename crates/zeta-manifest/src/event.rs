//! Defines event declarations and schema behavior.

use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};

use crate::error::{ManifestError, ManifestErrorKind};
use crate::spec::AgentSpec;

/// Holds the validated event vocabulary for one authored project.
///
/// Entries iterate in event-name order. A known event may omit its payload
/// schema, which remains distinct from an unknown event.
///
/// # Examples
///
/// ```
/// let mut events = zeta_manifest::EventRegistry::new();
/// events.register("work.requested", None)?;
/// assert!(events.knows("work.requested"));
/// # Ok::<(), zeta_manifest::ManifestError>(())
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
    /// let events = zeta_manifest::EventRegistry::new();
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
    /// Returns [`ManifestError`] when the event is empty, its schema is
    /// malformed, or it conflicts with an existing declaration.
    ///
    /// [`ManifestError`]: crate::ManifestError
    ///
    /// # Examples
    ///
    /// ```
    /// let mut events = zeta_manifest::EventRegistry::new();
    /// events.register("work.requested", None)?;
    /// events.register("work.requested", None)?;
    /// assert_eq!(events.iter().count(), 1);
    /// # Ok::<(), zeta_manifest::ManifestError>(())
    /// ```
    pub fn register(
        &mut self,
        event_type: &str,
        schema: Option<Map<String, Value>>,
    ) -> Result<(), ManifestError> {
        if event_type.is_empty() {
            return Err(ManifestError::new(
                ManifestErrorKind::InvalidSchema,
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
            return Err(ManifestError::new(
                ManifestErrorKind::ConflictingDeclaration,
                Some(event_type),
                None,
                "event is already registered with a different schema",
            ));
        }
        self.events.insert(event_type.to_owned(), schema);
        Ok(())
    }

    /// Registers the occurrence payload schema for one agent schedule.
    ///
    /// # Errors
    ///
    /// Returns [`ManifestError`] if the synthetic event conflicts with an
    /// existing event declaration.
    ///
    /// [`ManifestError`]: crate::ManifestError
    ///
    /// # Examples
    ///
    /// ```
    /// let mut events = zeta_manifest::EventRegistry::new();
    /// events.register_scheduled("digest")?;
    /// assert!(events.knows("agent.digest.scheduled"));
    /// # Ok::<(), zeta_manifest::ManifestError>(())
    /// ```
    pub fn register_scheduled(&mut self, agent_slug: &str) -> Result<(), ManifestError> {
        self.register(
            &scheduled_event_type(agent_slug),
            Some(scheduled_payload_schema()),
        )
    }

    /// Returns whether an event is present in the vocabulary.
    ///
    /// # Examples
    ///
    /// ```
    /// let mut events = zeta_manifest::EventRegistry::new();
    /// events.register("work.requested", None)?;
    /// assert!(events.knows("work.requested"));
    /// # Ok::<(), zeta_manifest::ManifestError>(())
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
    /// let mut events = zeta_manifest::EventRegistry::new();
    /// events.register("work.requested", None)?;
    /// assert_eq!(events.schema("work.requested"), Some(None));
    /// assert_eq!(events.schema("missing"), None);
    /// # Ok::<(), zeta_manifest::ManifestError>(())
    /// ```
    pub fn schema(&self, event_type: &str) -> Option<Option<&Map<String, Value>>> {
        self.events.get(event_type).map(Option::as_ref)
    }

    /// Iterates over events in deterministic name order.
    ///
    /// # Examples
    ///
    /// ```
    /// let mut events = zeta_manifest::EventRegistry::new();
    /// events.register("z.last", None)?;
    /// events.register("a.first", None)?;
    /// assert_eq!(events.iter().next().unwrap().0, "a.first");
    /// # Ok::<(), zeta_manifest::ManifestError>(())
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
/// Returns [`ManifestError`] when a returned event is not in `events`.
///
/// [`ManifestError`]: crate::ManifestError
///
/// # Examples
///
/// ```
/// let spec = zeta_manifest::parse_agent(
///     "worker",
///     b"---\nname: Worker\ndescription: Works.\nreturns: [work.completed]\n---\n",
/// )?;
/// let mut events = zeta_manifest::EventRegistry::new();
/// events.register("work.completed", None)?;
/// let schema = zeta_manifest::derive_returns_schema(&spec, &events)?.unwrap();
/// assert_eq!(schema["oneOf"][0]["properties"]["type"]["const"], "work.completed");
/// # Ok::<(), Box<dyn std::error::Error>>(())
/// ```
pub fn derive_returns_schema(
    spec: &AgentSpec,
    events: &EventRegistry,
) -> Result<Option<Value>, ManifestError> {
    if spec.returns.is_empty() {
        return Ok(None);
    }
    let mut branches = Vec::new();
    let mut definitions = Map::new();
    for (branch_index, event_type) in spec.returns.iter().enumerate() {
        let Some(schema) = events.schema(event_type) else {
            return Err(ManifestError::new(
                ManifestErrorKind::UnknownEvent,
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

pub(crate) fn validate_schema(
    event_type: &str,
    schema: &Map<String, Value>,
) -> Result<(), ManifestError> {
    let schema = Value::Object(schema.clone());
    if jsonschema::draft202012::meta::is_valid(&schema) {
        return Ok(());
    }
    Err(ManifestError::new(
        ManifestErrorKind::InvalidSchema,
        Some(event_type),
        None,
        "event payload schema is not a valid Draft 2020-12 schema",
    ))
}

fn scheduled_payload_schema() -> Map<String, Value> {
    Map::from_iter([
        ("type".to_owned(), Value::String("object".to_owned())),
        (
            "properties".to_owned(),
            Value::Object(Map::from_iter([
                (
                    "date".to_owned(),
                    Value::Object(Map::from_iter([(
                        "type".to_owned(),
                        Value::String("string".to_owned()),
                    )])),
                ),
                (
                    "timestamp".to_owned(),
                    Value::Object(Map::from_iter([(
                        "type".to_owned(),
                        Value::String("string".to_owned()),
                    )])),
                ),
            ])),
        ),
        (
            "required".to_owned(),
            Value::Array(vec![
                Value::String("date".to_owned()),
                Value::String("timestamp".to_owned()),
            ]),
        ),
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

/// Returns the synthetic event type used by an agent schedule.
///
/// # Examples
///
/// ```
/// assert_eq!(
///     zeta_manifest::scheduled_event_type("digest"),
///     "agent.digest.scheduled"
/// );
/// ```
pub fn scheduled_event_type(agent_slug: &str) -> String {
    format!("agent.{agent_slug}.scheduled")
}
