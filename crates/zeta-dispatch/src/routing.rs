//! Deterministic event route matching and session resolution.

use std::collections::HashMap;
use std::fmt;
use std::ops::Deref;
use std::str::FromStr;

use num_bigint::BigInt;
use rustpython_format::{
    CharLen, FieldName, FieldNamePart, FieldType, FormatPart, FormatSpec, FormatString,
    FromTemplate,
};
use serde_json::Value;
use zeta_journal::Event;

use crate::identity::{queue_item_id, QueueItemId, SessionId};

/// Matches an event type with case-sensitive shell-style glob semantics.
///
/// # Examples
///
/// ```
/// let pattern = zeta_dispatch::EventPattern::new("orders.*");
/// let event = zeta_journal::Event {
///     id: "evt_1".to_owned(),
///     event_type: "orders.created".to_owned(),
///     source: "shop".to_owned(),
///     payload: serde_json::Map::new(),
///     idempotency_key: None,
///     caused_by: None,
///     session_id: None,
///     run_id: None,
///     turn_id: None,
///     timestamp_ms: 1,
///     cursor: None,
/// };
/// assert!(pattern.matches(&event));
/// ```
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct EventPattern {
    text: String,
    kind: PatternKind,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum PatternKind {
    Exact,
    ShellGlob,
}

impl EventPattern {
    /// Creates a pattern from its authored text.
    pub fn new(pattern: impl Into<String>) -> Self {
        EventPattern {
            text: pattern.into(),
            kind: PatternKind::ShellGlob,
        }
    }

    /// Creates an exact event type accepted by a normalized authored agent.
    pub fn exact(event_type: impl Into<String>) -> Self {
        EventPattern {
            text: event_type.into(),
            kind: PatternKind::Exact,
        }
    }

    /// Returns the authored pattern text.
    pub fn as_str(&self) -> &str {
        &self.text
    }

    /// Reports whether the event type satisfies this pattern.
    pub fn matches(&self, event: &Event) -> bool {
        match self.kind {
            PatternKind::Exact => self.text == event.event_type,
            PatternKind::ShellGlob => shell_glob_matches(&self.text, &event.event_type),
        }
    }
}

/// Selects how an authored agent scopes durable session history.
///
/// The rule is resolved before a queue item becomes executable, so retries
/// keep the same durable session identity.
#[derive(Clone, Debug, Eq, PartialEq)]
pub enum SessionRule {
    /// Every event accepted by the agent shares one session.
    Shared,
    /// Every accepted event starts a distinct session.
    PerEvent,
    /// Event fields render a stable session suffix.
    Template(String),
}

impl SessionRule {
    /// Resolves the full agent session id before execution begins.
    ///
    /// # Errors
    ///
    /// Returns [`SessionError`] when a template is malformed, references a
    /// missing field, or applies an invalid Python format operation.
    pub fn resolve(&self, agent_id: &str, event: &Event) -> Result<SessionId, SessionError> {
        let suffix = match self {
            SessionRule::Shared => None,
            SessionRule::PerEvent => Some(event.id.clone()),
            SessionRule::Template(template) => Some(render_event_template(template, event)?),
        };
        Ok(SessionId::for_agent(agent_id, suffix.as_deref()))
    }
}

impl FromStr for SessionRule {
    type Err = SessionError;

    fn from_str(text: &str) -> Result<Self, Self::Err> {
        let rule = if text == "shared" {
            SessionRule::Shared
        } else if text == "per-event" {
            SessionRule::PerEvent
        } else {
            SessionRule::Template(text.to_owned())
        };
        Ok(rule)
    }
}

/// Describes one immutable event-to-agent route.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Route {
    agent_id: String,
    accepts: Vec<EventPattern>,
    session: SessionRule,
    lock_keys: Vec<String>,
    project_revision: Option<String>,
}

impl Route {
    /// Creates a route whose declaration order is retained by route planning.
    pub fn new(
        agent_id: impl Into<String>,
        accepts: Vec<EventPattern>,
        session: SessionRule,
        lock_keys: Vec<String>,
        project_revision: Option<String>,
    ) -> Self {
        Route {
            agent_id: agent_id.into(),
            accepts,
            session,
            lock_keys,
            project_revision,
        }
    }

    /// Returns the target agent id.
    pub fn agent_id(&self) -> &str {
        &self.agent_id
    }

    /// Reports whether at least one accepted pattern matches the event.
    pub fn matches(&self, event: &Event) -> bool {
        for pattern in &self.accepts {
            if pattern.matches(event) {
                return true;
            }
        }
        false
    }
}

/// Carries one deterministic event-to-agent binding.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RouteDecision {
    agent_id: String,
    queue_item_id: QueueItemId,
    session_id: SessionId,
    lock_keys: Vec<String>,
    project_revision: Option<String>,
}

impl RouteDecision {
    pub(crate) fn bind_queue_item_id(&mut self, queue_item_id: QueueItemId) {
        self.queue_item_id = queue_item_id;
    }

    /// Returns the selected agent id.
    pub fn agent_id(&self) -> &str {
        &self.agent_id
    }

    /// Returns the retry-stable queue item identity.
    pub fn queue_item_id(&self) -> &QueueItemId {
        &self.queue_item_id
    }

    /// Returns the session resolved before execution.
    pub fn session_id(&self) -> &SessionId {
        &self.session_id
    }

    /// Returns every authored exclusion key in declaration order.
    pub fn lock_keys(&self) -> &[String] {
        &self.lock_keys
    }

    /// Returns the stored project revision when the route supplied one.
    pub fn project_revision(&self) -> Option<&str> {
        self.project_revision.as_deref()
    }
}

/// Plans every matching route while preserving declaration order.
///
/// # Errors
///
/// Returns [`SessionError`] if a matched route cannot resolve its session.
pub fn route_event(event: &Event, routes: &[Route]) -> Result<Vec<RouteDecision>, SessionError> {
    let mut decisions = Vec::new();
    for route in routes {
        if !route.matches(event) {
            continue;
        }
        decisions.push(RouteDecision {
            agent_id: route.agent_id.clone(),
            queue_item_id: queue_item_id(&event.id, &route.agent_id),
            session_id: route.session.resolve(&route.agent_id, event)?,
            lock_keys: route.lock_keys.clone(),
            project_revision: route.project_revision.clone(),
        });
    }
    if decisions.len() == 1 {
        decisions[0].queue_item_id = crate::identity::pending_queue_item_id(&event.id);
    }
    Ok(decisions)
}

/// Reports why a session template cannot produce a stable string suffix.
#[derive(Clone, Debug, Eq, PartialEq)]
pub enum SessionError {
    /// A replacement field has no closing brace or a lone closing brace exists.
    MalformedTemplate(String),
    /// The requested event or payload field does not exist.
    MissingField(String),
    /// The requested indexing or attribute operation is invalid for its value.
    InvalidField(String),
    /// A conversion or format specification is invalid for its value.
    InvalidFormat(String),
}

impl fmt::Display for SessionError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            SessionError::MalformedTemplate(template) => {
                write!(formatter, "malformed session template {template:?}")
            }
            SessionError::MissingField(field) => {
                write!(
                    formatter,
                    "session template references missing field {field:?}"
                )
            }
            SessionError::InvalidField(field) => {
                write!(formatter, "invalid session template field {field:?}")
            }
            SessionError::InvalidFormat(specification) => {
                write!(formatter, "invalid session format {specification:?}")
            }
        }
    }
}

impl std::error::Error for SessionError {}

/// Renders one event-field template for a stable runtime key.
///
/// The renderer uses the same field and format rules as route session keys.
pub fn render_event_template(template: &str, event: &Event) -> Result<String, SessionError> {
    render_template_text(template, event, 0)
}

fn render_template_text(
    template: &str,
    event: &Event,
    nesting: usize,
) -> Result<String, SessionError> {
    if nesting > 2 {
        return Err(SessionError::MalformedTemplate(template.to_owned()));
    }
    let format = <FormatString as FromTemplate>::from_str(template)
        .map_err(|_error| SessionError::MalformedTemplate(template.to_owned()))?;
    let mut rendered = String::new();
    for part in format.format_parts {
        match part {
            FormatPart::Literal(literal) => rendered.push_str(&literal),
            FormatPart::Field {
                field_name,
                conversion_spec,
                format_spec,
            } => {
                let value = resolve_template_field(&field_name, event)?;
                let format_spec = if format_spec.contains(['{', '}']) {
                    render_template_text(&format_spec, event, nesting + 1)?
                } else {
                    format_spec
                };
                let value =
                    format_template_value(&field_name, value, conversion_spec, &format_spec)?;
                rendered.push_str(&value);
            }
        }
    }
    Ok(rendered)
}

enum TemplateValue {
    Json(Value),
    Event(String),
}

fn resolve_template_field(field: &str, event: &Event) -> Result<TemplateValue, SessionError> {
    let field_name =
        FieldName::parse(field).map_err(|_error| SessionError::InvalidField(field.to_owned()))?;
    let FieldName { field_type, parts } = field_name;
    let root = match field_type {
        FieldType::Auto => return Err(SessionError::MissingField(field.to_owned())),
        FieldType::Index(_index) => return Err(SessionError::MissingField(field.to_owned())),
        FieldType::Keyword(keyword) => keyword,
    };
    if root == "event" {
        if parts.is_empty() {
            return Ok(TemplateValue::Event(event_repr(event, false)));
        }
        let mut parts = parts.into_iter();
        let Some(first) = parts.next() else {
            return Ok(TemplateValue::Event(event_repr(event, false)));
        };
        let FieldNamePart::Attribute(attribute) = first else {
            return Err(SessionError::InvalidField(field.to_owned()));
        };
        let Some(mut value) = event_attribute(event, &attribute) else {
            return Err(SessionError::MissingField(field.to_owned()));
        };
        for part in parts {
            value = index_template_value(field, value, part)?;
        }
        return Ok(TemplateValue::Json(value));
    }

    let Some(mut value) = event
        .payload
        .get(&root)
        .cloned()
        .or_else(|| event_attribute(event, &root))
    else {
        return Err(SessionError::MissingField(field.to_owned()));
    };
    for part in parts {
        value = index_template_value(field, value, part)?;
    }
    Ok(TemplateValue::Json(value))
}

fn event_attribute(event: &Event, attribute: &str) -> Option<Value> {
    if attribute == "id" {
        Some(Value::String(event.id.clone()))
    } else if attribute == "event_type" {
        Some(Value::String(event.event_type.clone()))
    } else if attribute == "source" {
        Some(Value::String(event.source.clone()))
    } else if attribute == "payload" {
        Some(Value::Object(event.payload.clone()))
    } else if attribute == "idempotency_key" {
        Some(optional_string_value(&event.idempotency_key))
    } else if attribute == "caused_by" {
        Some(optional_string_value(&event.caused_by))
    } else if attribute == "session_id" {
        Some(optional_string_value(&event.session_id))
    } else if attribute == "run_id" {
        Some(optional_string_value(&event.run_id))
    } else if attribute == "turn_id" {
        Some(optional_string_value(&event.turn_id))
    } else if attribute == "timestamp_ms" {
        Some(Value::from(event.timestamp_ms))
    } else if attribute == "cursor" {
        Some(event.cursor.map(Value::from).unwrap_or(Value::Null))
    } else {
        None
    }
}

fn optional_string_value(value: &Option<String>) -> Value {
    match value {
        Some(value) => Value::String(value.clone()),
        None => Value::Null,
    }
}

fn index_template_value(
    field: &str,
    value: Value,
    part: FieldNamePart,
) -> Result<Value, SessionError> {
    match part {
        FieldNamePart::Attribute(_attribute) => Err(SessionError::InvalidField(field.to_owned())),
        FieldNamePart::Index(index) => match value {
            Value::Array(values) => values
                .get(index)
                .cloned()
                .ok_or_else(|| SessionError::MissingField(field.to_owned())),
            Value::String(text) => string_character(&text, index)
                .map(|character| Value::String(character.to_string()))
                .ok_or_else(|| SessionError::MissingField(field.to_owned())),
            Value::Null => Err(SessionError::InvalidField(field.to_owned())),
            Value::Bool(_value) => Err(SessionError::InvalidField(field.to_owned())),
            Value::Number(_value) => Err(SessionError::InvalidField(field.to_owned())),
            Value::Object(_value) => Err(SessionError::InvalidField(field.to_owned())),
        },
        FieldNamePart::StringIndex(index) => match value {
            Value::Object(values) => values
                .get(&index)
                .cloned()
                .ok_or_else(|| SessionError::MissingField(field.to_owned())),
            Value::Null => Err(SessionError::InvalidField(field.to_owned())),
            Value::Bool(_value) => Err(SessionError::InvalidField(field.to_owned())),
            Value::Number(_value) => Err(SessionError::InvalidField(field.to_owned())),
            Value::String(_value) => Err(SessionError::InvalidField(field.to_owned())),
            Value::Array(_value) => Err(SessionError::InvalidField(field.to_owned())),
        },
    }
}

fn string_character(text: &str, expected_index: usize) -> Option<char> {
    for (index, character) in text.chars().enumerate() {
        if index == expected_index {
            return Some(character);
        }
    }
    None
}

fn format_template_value(
    field: &str,
    value: TemplateValue,
    conversion: Option<char>,
    format_spec: &str,
) -> Result<String, SessionError> {
    let spec = FormatSpec::parse(format_spec)
        .map_err(|error| SessionError::InvalidFormat(format!("{field}:{error:?}")))?;
    match value {
        TemplateValue::Event(representation) => {
            if conversion.is_none() && !format_spec.is_empty() {
                return Err(SessionError::InvalidFormat(field.to_owned()));
            }
            let representation = if conversion == Some('a') {
                ascii_escape(&representation)
            } else if conversion.is_none() || conversion == Some('s') || conversion == Some('r') {
                representation
            } else {
                return Err(SessionError::InvalidFormat(field.to_owned()));
            };
            spec.format_string(&TemplateString(representation))
                .map_err(|error| SessionError::InvalidFormat(format!("{field}:{error:?}")))
        }
        TemplateValue::Json(value) => {
            if let Some(conversion) = conversion {
                let converted = if conversion == 's' {
                    python_str(&value)
                } else if conversion == 'r' {
                    python_repr(&value, false)
                } else if conversion == 'a' {
                    python_repr(&value, true)
                } else {
                    return Err(SessionError::InvalidFormat(field.to_owned()));
                };
                return spec
                    .format_string(&TemplateString(converted))
                    .map_err(|error| SessionError::InvalidFormat(format!("{field}:{error:?}")));
            }
            format_json_value(field, value, spec, format_spec)
        }
    }
}

fn format_json_value(
    field: &str,
    value: Value,
    spec: FormatSpec,
    raw_spec: &str,
) -> Result<String, SessionError> {
    let result = match value {
        Value::Null => {
            if !raw_spec.is_empty() {
                return Err(SessionError::InvalidFormat(field.to_owned()));
            }
            Ok("None".to_owned())
        }
        Value::Bool(value) => spec.format_bool(value),
        Value::Number(number) => {
            if let Some(value) = number.as_i64() {
                spec.format_int(&BigInt::from(value))
            } else if let Some(value) = number.as_u64() {
                spec.format_int(&BigInt::from(value))
            } else if let Some(value) = number.as_f64() {
                spec.format_float(value)
            } else {
                return Err(SessionError::InvalidFormat(field.to_owned()));
            }
        }
        Value::String(value) => spec.format_string(&TemplateString(value)),
        Value::Array(values) => {
            if !raw_spec.is_empty() {
                return Err(SessionError::InvalidFormat(field.to_owned()));
            }
            Ok(python_repr(&Value::Array(values), false))
        }
        Value::Object(values) => {
            if !raw_spec.is_empty() {
                return Err(SessionError::InvalidFormat(field.to_owned()));
            }
            Ok(python_repr(&Value::Object(values), false))
        }
    };
    result.map_err(|error| SessionError::InvalidFormat(format!("{field}:{error:?}")))
}

fn python_str(value: &Value) -> String {
    match value {
        Value::String(value) => value.clone(),
        Value::Null => python_repr(value, false),
        Value::Bool(_value) => python_repr(value, false),
        Value::Number(_value) => python_repr(value, false),
        Value::Array(_value) => python_repr(value, false),
        Value::Object(_value) => python_repr(value, false),
    }
}

fn python_repr(value: &Value, ascii_only: bool) -> String {
    match value {
        Value::Null => "None".to_owned(),
        Value::Bool(true) => "True".to_owned(),
        Value::Bool(false) => "False".to_owned(),
        Value::Number(number) => number.to_string(),
        Value::String(value) => python_quote(value, ascii_only),
        Value::Array(values) => {
            let mut result = String::from("[");
            let mut first = true;
            for value in values {
                if !first {
                    result.push_str(", ");
                }
                result.push_str(&python_repr(value, ascii_only));
                first = false;
            }
            result.push(']');
            result
        }
        Value::Object(values) => {
            let mut result = String::from("{");
            let mut first = true;
            for (key, value) in values {
                if !first {
                    result.push_str(", ");
                }
                result.push_str(&python_quote(key, ascii_only));
                result.push_str(": ");
                result.push_str(&python_repr(value, ascii_only));
                first = false;
            }
            result.push('}');
            result
        }
    }
}

fn python_quote(text: &str, ascii_only: bool) -> String {
    let quote = if text.contains('\'') && !text.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut result = String::new();
    result.push(quote);
    for character in text.chars() {
        if character == quote || character == '\\' {
            result.push('\\');
            result.push(character);
        } else if character == '\t' {
            result.push_str("\\t");
        } else if character == '\n' {
            result.push_str("\\n");
        } else if character == '\r' {
            result.push_str("\\r");
        } else if character.is_control() || (ascii_only && !character.is_ascii()) {
            push_python_escape(&mut result, character);
        } else {
            result.push(character);
        }
    }
    result.push(quote);
    result
}

fn push_python_escape(result: &mut String, character: char) {
    let code = u32::from(character);
    if code <= 0xff {
        result.push_str(&format!("\\x{code:02x}"));
    } else if code <= 0xffff {
        result.push_str(&format!("\\u{code:04x}"));
    } else {
        result.push_str(&format!("\\U{code:08x}"));
    }
}

fn ascii_escape(text: &str) -> String {
    let mut result = String::new();
    for character in text.chars() {
        if character.is_ascii() {
            result.push(character);
        } else {
            push_python_escape(&mut result, character);
        }
    }
    result
}

fn event_repr(event: &Event, ascii_only: bool) -> String {
    let mut result = String::from("Event(id=");
    result.push_str(&python_quote(&event.id, ascii_only));
    result.push_str(", event_type=");
    result.push_str(&python_quote(&event.event_type, ascii_only));
    result.push_str(", source=");
    result.push_str(&python_quote(&event.source, ascii_only));
    result.push_str(", payload=");
    result.push_str(&python_repr(
        &Value::Object(event.payload.clone()),
        ascii_only,
    ));
    result.push_str(", idempotency_key=");
    result.push_str(&python_repr(
        &optional_string_value(&event.idempotency_key),
        ascii_only,
    ));
    result.push_str(", caused_by=");
    result.push_str(&python_repr(
        &optional_string_value(&event.caused_by),
        ascii_only,
    ));
    result.push_str(", session_id=");
    result.push_str(&python_repr(
        &optional_string_value(&event.session_id),
        ascii_only,
    ));
    result.push_str(", timestamp_ms=");
    result.push_str(&event.timestamp_ms.to_string());
    result.push_str(", turn_id=");
    result.push_str(&python_repr(
        &optional_string_value(&event.turn_id),
        ascii_only,
    ));
    result.push_str(", run_id=");
    result.push_str(&python_repr(
        &optional_string_value(&event.run_id),
        ascii_only,
    ));
    result.push_str(", cursor=");
    let cursor = event.cursor.map(Value::from).unwrap_or(Value::Null);
    result.push_str(&python_repr(&cursor, ascii_only));
    result.push(')');
    result
}

struct TemplateString(String);

impl CharLen for TemplateString {
    fn char_len(&self) -> usize {
        let mut length = 0;
        for _character in self.0.chars() {
            length += 1;
        }
        length
    }
}

impl Deref for TemplateString {
    type Target = str;

    fn deref(&self) -> &Self::Target {
        &self.0
    }
}

fn shell_glob_matches(pattern: &str, candidate: &str) -> bool {
    let pattern: Vec<char> = pattern.chars().collect();
    let candidate: Vec<char> = candidate.chars().collect();
    let mut memo = HashMap::new();
    glob_suffix_matches(&pattern, &candidate, 0, 0, &mut memo)
}

fn glob_suffix_matches(
    pattern: &[char],
    candidate: &[char],
    pattern_index: usize,
    candidate_index: usize,
    memo: &mut HashMap<(usize, usize), bool>,
) -> bool {
    if let Some(answer) = memo.get(&(pattern_index, candidate_index)) {
        return *answer;
    }
    let answer = if pattern_index == pattern.len() {
        candidate_index == candidate.len()
    } else if pattern[pattern_index] == '*' {
        glob_suffix_matches(pattern, candidate, pattern_index + 1, candidate_index, memo)
            || (candidate_index < candidate.len()
                && glob_suffix_matches(
                    pattern,
                    candidate,
                    pattern_index,
                    candidate_index + 1,
                    memo,
                ))
    } else if candidate_index == candidate.len() {
        false
    } else if pattern[pattern_index] == '?' {
        glob_suffix_matches(
            pattern,
            candidate,
            pattern_index + 1,
            candidate_index + 1,
            memo,
        )
    } else if pattern[pattern_index] == '[' {
        match character_class(pattern, pattern_index, candidate[candidate_index]) {
            Some((class_end, true)) => {
                glob_suffix_matches(pattern, candidate, class_end + 1, candidate_index + 1, memo)
            }
            Some((_class_end, false)) => false,
            None => {
                pattern[pattern_index] == candidate[candidate_index]
                    && glob_suffix_matches(
                        pattern,
                        candidate,
                        pattern_index + 1,
                        candidate_index + 1,
                        memo,
                    )
            }
        }
    } else {
        pattern[pattern_index] == candidate[candidate_index]
            && glob_suffix_matches(
                pattern,
                candidate,
                pattern_index + 1,
                candidate_index + 1,
                memo,
            )
    };
    memo.insert((pattern_index, candidate_index), answer);
    answer
}

fn character_class(pattern: &[char], start: usize, candidate: char) -> Option<(usize, bool)> {
    let mut index = start + 1;
    let negated = pattern.get(index) == Some(&'!');
    if negated {
        index += 1;
    }
    let content_start = index;
    if pattern.get(index) == Some(&']') {
        index += 1;
    }
    while index < pattern.len() && pattern[index] != ']' {
        index += 1;
    }
    if index == pattern.len() || index == content_start {
        return None;
    }

    let mut matched = false;
    let mut class_index = content_start;
    while class_index < index {
        let first = pattern[class_index];
        if class_index + 2 < index && pattern[class_index + 1] == '-' {
            let last = pattern[class_index + 2];
            if first <= candidate && candidate <= last {
                matched = true;
            }
            class_index += 3;
        } else {
            if first == candidate {
                matched = true;
            }
            class_index += 1;
        }
    }
    if negated {
        matched = !matched;
    }
    Some((index, matched))
}
