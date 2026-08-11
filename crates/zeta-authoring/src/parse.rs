//! Parses authored agent Markdown into declaration values.

use std::path::{Path, PathBuf};

use serde_json::{Map, Number, Value};
use yaml_serde::Value as YamlValue;
use zeta::substrate::hash_bytes;

use crate::error::{SpecError, SpecErrorKind};
use crate::spec::{
    scheduled_event_type, AgentSpec, EgressBinding, ExecutorSpec, IngressBinding, ModelSpec,
    RetrySpec, ScheduleEntry,
};

/// Parses one authored agent from exact UTF-8 Markdown bytes.
///
/// The function performs no filesystem access. `path` supplies the logical
/// filename used for the slug and diagnostics.
///
/// # Errors
///
/// Returns [`SpecError`] when the source, frontmatter, slug, or declaration
/// values are invalid.
///
/// [`SpecError`]: crate::SpecError
///
/// # Examples
///
/// ```
/// use std::path::Path;
///
/// let spec = zeta_authoring::parse_agent(
///     Path::new("worker.md"),
///     b"---\nname: Worker\ndescription: Does work.\n---\nWork.\n",
/// )?;
/// assert_eq!(spec.instructions, "Work.\n");
/// # Ok::<(), zeta_authoring::SpecError>(())
/// ```
pub fn parse_agent(path: &Path, source: &[u8]) -> Result<AgentSpec, SpecError> {
    let content = std::str::from_utf8(source).map_err(|error| {
        SpecError::new(SpecErrorKind::InvalidUtf8, None, path, error.to_string())
    })?;
    let (frontmatter, instructions) = split_frontmatter(content, path)?;
    let mut frontmatter = parse_frontmatter(frontmatter, path)?;
    let slug = slug_from_path(path)?;

    let skills_inherit = !frontmatter.contains_key("skills");
    let tools_inherit = !frontmatter.contains_key("tools");
    let name = take_required_string(&mut frontmatter, "name", path)?;
    let description = take_required_string(&mut frontmatter, "description", path)?;
    let enabled = take_bool(&mut frontmatter, "enabled", true, path)?;
    let session = take_session(&mut frontmatter, path)?;
    let model = take_model(&mut frontmatter, path)?;
    let executor = take_executor(&mut frontmatter, path)?;
    let (mut accepts, ingress) = take_accepts(&mut frontmatter, path)?;
    let (publishes, egress) = take_publishes(&mut frontmatter, path)?;
    let returns = take_string_list(&mut frontmatter, "returns", path)?;
    let skills = take_string_list(&mut frontmatter, "skills", path)?;
    let tools = take_string_list(&mut frontmatter, "tools", path)?;
    let schedules = take_schedules(&mut frontmatter, path)?;
    let retry = take_retry(&mut frontmatter, path)?;
    let base_dir = take_base_dir(&mut frontmatter, path)?;

    if !schedules.is_empty() {
        let scheduled = scheduled_event_type(&slug);
        if !accepts.contains(&scheduled) {
            accepts.push(scheduled);
        }
    }

    Ok(AgentSpec {
        slug,
        name,
        description,
        instructions: instructions.to_owned(),
        path: path.to_path_buf(),
        content_address: hash_bytes(source),
        enabled,
        session,
        model,
        executor,
        accepts,
        publishes,
        returns,
        skills,
        skills_inherit,
        tools,
        tools_inherit,
        schedules,
        retry,
        base_dir,
        ingress,
        egress,
        extensions: frontmatter,
    })
}

fn split_frontmatter<'a>(content: &'a str, path: &Path) -> Result<(&'a str, &'a str), SpecError> {
    let mut lines = content.split_inclusive('\n');
    let Some(first) = lines.next() else {
        return Err(SpecError::new(
            SpecErrorKind::MissingFrontmatterDelimiter,
            None,
            path,
            "the first line must be ---",
        ));
    };
    if first.trim() != "---" {
        return Err(SpecError::new(
            SpecErrorKind::MissingFrontmatterDelimiter,
            None,
            path,
            "the first line must be ---",
        ));
    }

    let frontmatter_start = first.len();
    let mut line_start = frontmatter_start;
    for line in lines {
        if line.trim() == "---" {
            let frontmatter = &content[frontmatter_start..line_start];
            let instructions = &content[line_start + line.len()..];
            return Ok((frontmatter, instructions));
        }
        line_start += line.len();
    }
    Err(SpecError::new(
        SpecErrorKind::MissingClosingFrontmatterDelimiter,
        None,
        path,
        "frontmatter must end with ---",
    ))
}

fn parse_frontmatter(source: &str, path: &Path) -> Result<Map<String, Value>, SpecError> {
    let source = yaml_serde::from_str::<YamlValue>(source).map_err(|error| {
        SpecError::new(SpecErrorKind::InvalidYaml, None, path, error.to_string())
    })?;
    let mapping = match source {
        YamlValue::Null => return Ok(Map::new()),
        YamlValue::Mapping(mapping) => mapping,
        YamlValue::Bool(_)
        | YamlValue::Number(_)
        | YamlValue::String(_)
        | YamlValue::Sequence(_)
        | YamlValue::Tagged(_) => {
            return Err(SpecError::new(
                SpecErrorKind::ExpectedFrontmatterObject,
                None,
                path,
                "frontmatter must be an object",
            ));
        }
    };

    let mut output = Map::new();
    for (key, value) in mapping {
        let YamlValue::String(key) = key else {
            return Err(SpecError::new(
                SpecErrorKind::ExpectedFrontmatterObject,
                None,
                path,
                "frontmatter keys must be strings",
            ));
        };
        if key == "<<" {
            return Err(SpecError::new(
                SpecErrorKind::InvalidField,
                Some(&key),
                path,
                "merge keys are not supported",
            ));
        }
        let value = yaml_to_json(value).map_err(|detail| {
            SpecError::new(SpecErrorKind::InvalidField, Some(&key), path, detail)
        })?;
        output.insert(key, value);
    }
    Ok(output)
}

fn yaml_to_json(value: YamlValue) -> Result<Value, &'static str> {
    match value {
        YamlValue::Null => Ok(Value::Null),
        YamlValue::Bool(value) => Ok(Value::Bool(value)),
        YamlValue::Number(value) => yaml_number_to_json(&value),
        YamlValue::String(value) => Ok(Value::String(value)),
        YamlValue::Sequence(values) => {
            let mut output = Vec::with_capacity(values.len());
            for value in values {
                output.push(yaml_to_json(value)?);
            }
            Ok(Value::Array(output))
        }
        YamlValue::Mapping(values) => {
            let mut output = Map::new();
            for (key, value) in values {
                let YamlValue::String(key) = key else {
                    return Err("object keys must be strings");
                };
                if key == "<<" {
                    return Err("merge keys are not supported");
                }
                output.insert(key, yaml_to_json(value)?);
            }
            Ok(Value::Object(output))
        }
        YamlValue::Tagged(_) => Err("tagged YAML values are not supported"),
    }
}

fn yaml_number_to_json(value: &yaml_serde::Number) -> Result<Value, &'static str> {
    if let Some(value) = value.as_i64() {
        return Ok(Value::Number(Number::from(value)));
    }
    if let Some(value) = value.as_u64() {
        return Ok(Value::Number(Number::from(value)));
    }
    let Some(value) = value.as_f64() else {
        return Err("number is outside the JSON numeric domain");
    };
    if !value.is_finite() {
        return Err("numbers must be finite");
    }
    let Some(value) = Number::from_f64(value) else {
        return Err("number is outside the JSON numeric domain");
    };
    Ok(Value::Number(value))
}

fn slug_from_path(path: &Path) -> Result<String, SpecError> {
    let Some(stem) = path.file_stem().and_then(|stem| stem.to_str()) else {
        return Err(SpecError::new(
            SpecErrorKind::InvalidSlug,
            None,
            path,
            "the logical filename must have a UTF-8 stem",
        ));
    };
    if stem.is_empty() {
        return Err(SpecError::new(
            SpecErrorKind::InvalidSlug,
            None,
            path,
            "the filename stem must match [a-z0-9_-]+",
        ));
    }
    for byte in stem.bytes() {
        let valid =
            byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'_' || byte == b'-';
        if !valid {
            return Err(SpecError::new(
                SpecErrorKind::InvalidSlug,
                None,
                path,
                "the filename stem must match [a-z0-9_-]+",
            ));
        }
    }
    Ok(stem.to_owned())
}

fn take_required_string(
    values: &mut Map<String, Value>,
    field: &'static str,
    path: &Path,
) -> Result<String, SpecError> {
    let value = values.remove(field);
    let Some(Value::String(value)) = value else {
        return Err(SpecError::new(
            SpecErrorKind::MissingRequiredField,
            Some(field),
            path,
            "a non-empty string is required",
        ));
    };
    if value.is_empty() {
        return Err(SpecError::new(
            SpecErrorKind::MissingRequiredField,
            Some(field),
            path,
            "a non-empty string is required",
        ));
    }
    Ok(value)
}

fn take_bool(
    values: &mut Map<String, Value>,
    field: &'static str,
    default: bool,
    path: &Path,
) -> Result<bool, SpecError> {
    match values.remove(field) {
        None => Ok(default),
        Some(Value::Bool(value)) => Ok(value),
        Some(
            Value::Null | Value::Number(_) | Value::String(_) | Value::Array(_) | Value::Object(_),
        ) => Err(invalid_field(path, field, "expected a boolean")),
    }
}

fn take_session(values: &mut Map<String, Value>, path: &Path) -> Result<String, SpecError> {
    match values.remove("session") {
        None | Some(Value::Null) => Ok("per-event".to_owned()),
        Some(Value::String(value)) => {
            if value == "shared" || value == "per-event" || value.contains('{') {
                Ok(value)
            } else {
                Err(invalid_field(
                    path,
                    "session",
                    "expected shared, per-event, or a template",
                ))
            }
        }
        Some(Value::Bool(_) | Value::Number(_) | Value::Array(_) | Value::Object(_)) => {
            Err(invalid_field(path, "session", "expected a string"))
        }
    }
}

fn take_model(
    values: &mut Map<String, Value>,
    path: &Path,
) -> Result<Option<ModelSpec>, SpecError> {
    let value = values.remove("model");
    let Some(value) = value else {
        return Ok(None);
    };
    if value == Value::Null {
        return Ok(None);
    }
    let Value::Object(mut value) = value else {
        return Err(invalid_field(path, "model", "expected an object"));
    };
    reject_unknown_fields(&value, &["name", "url"], "model", path)?;
    let name = take_nested_required_string(&mut value, "name", "model", path)?;
    let url = take_nested_required_string(&mut value, "url", "model", path)?;
    Ok(Some(ModelSpec { name, url }))
}

fn take_executor(values: &mut Map<String, Value>, path: &Path) -> Result<ExecutorSpec, SpecError> {
    let value = values.remove("executor");
    let Some(value) = value else {
        return Ok(ExecutorSpec::default());
    };
    if value == Value::Null {
        return Ok(ExecutorSpec::default());
    }
    let Value::Object(mut value) = value else {
        return Err(invalid_field(path, "executor", "expected an object"));
    };
    reject_unknown_fields(&value, &["provider", "config"], "executor", path)?;
    let provider = take_nested_required_string(&mut value, "provider", "executor", path)?;
    let config = match value.remove("config") {
        None => Map::new(),
        Some(Value::Object(config)) => config,
        Some(
            Value::Null | Value::Bool(_) | Value::Number(_) | Value::String(_) | Value::Array(_),
        ) => return Err(invalid_field(path, "executor", "config must be an object")),
    };
    Ok(ExecutorSpec { provider, config })
}

fn take_nested_required_string(
    values: &mut Map<String, Value>,
    name: &str,
    parent: &'static str,
    path: &Path,
) -> Result<String, SpecError> {
    let value = values.remove(name);
    let Some(Value::String(value)) = value else {
        return Err(invalid_field(
            path,
            parent,
            format!("{name} must be a non-empty string"),
        ));
    };
    if value.is_empty() {
        return Err(invalid_field(
            path,
            parent,
            format!("{name} must be a non-empty string"),
        ));
    }
    Ok(value)
}

fn take_string_list(
    values: &mut Map<String, Value>,
    field: &'static str,
    path: &Path,
) -> Result<Vec<String>, SpecError> {
    let value = values.remove(field);
    let Some(value) = value else {
        return Ok(Vec::new());
    };
    if value == Value::Null {
        return Ok(Vec::new());
    }
    let Value::Array(values) = value else {
        return Err(invalid_field(path, field, "expected an array"));
    };
    let mut output = Vec::with_capacity(values.len());
    for value in values {
        let Value::String(value) = value else {
            return Err(invalid_field(
                path,
                field,
                "items must be non-empty strings",
            ));
        };
        if value.is_empty() {
            return Err(invalid_field(
                path,
                field,
                "items must be non-empty strings",
            ));
        }
        output.push(value);
    }
    Ok(output)
}

fn take_accepts(
    values: &mut Map<String, Value>,
    path: &Path,
) -> Result<(Vec<String>, Vec<IngressBinding>), SpecError> {
    let entries = take_event_entries(values, "accepts", path)?;
    let mut events = Vec::with_capacity(entries.len());
    let mut bindings = Vec::new();
    for entry in entries {
        match entry {
            Value::String(event) if !event.is_empty() => events.push(event),
            Value::Object(mut entry) => {
                reject_unknown_fields(
                    &entry,
                    &["event", "filter", "idempotency_key"],
                    "accepts",
                    path,
                )?;
                let event = take_nested_required_string(&mut entry, "event", "accepts", path)?;
                let filter = take_object(&mut entry, "filter", "accepts", path)?;
                let idempotency_key =
                    take_optional_string(&mut entry, "idempotency_key", "accepts", path)?;
                events.push(event.clone());
                bindings.push(IngressBinding {
                    event,
                    filter,
                    idempotency_key,
                });
            }
            Value::Null
            | Value::Bool(_)
            | Value::Number(_)
            | Value::String(_)
            | Value::Array(_) => {
                return Err(invalid_field(
                    path,
                    "accepts",
                    "items must be non-empty strings or objects",
                ));
            }
        }
    }
    Ok((events, bindings))
}

fn take_publishes(
    values: &mut Map<String, Value>,
    path: &Path,
) -> Result<(Vec<String>, Vec<EgressBinding>), SpecError> {
    let entries = take_event_entries(values, "publishes", path)?;
    let mut events = Vec::with_capacity(entries.len());
    let mut bindings = Vec::new();
    for entry in entries {
        match entry {
            Value::String(event) if !event.is_empty() => events.push(event),
            Value::Object(mut entry) => {
                if entry.contains_key("filter") {
                    return Err(invalid_field(
                        path,
                        "publishes",
                        "published event options use 'with'",
                    ));
                }
                reject_unknown_fields(
                    &entry,
                    &["event", "with", "idempotency_key"],
                    "publishes",
                    path,
                )?;
                let event = take_nested_required_string(&mut entry, "event", "publishes", path)?;
                let options = take_object(&mut entry, "with", "publishes", path)?;
                let idempotency_key =
                    take_optional_string(&mut entry, "idempotency_key", "publishes", path)?;
                events.push(event.clone());
                bindings.push(EgressBinding {
                    event,
                    options,
                    idempotency_key,
                });
            }
            Value::Null
            | Value::Bool(_)
            | Value::Number(_)
            | Value::String(_)
            | Value::Array(_) => {
                return Err(invalid_field(
                    path,
                    "publishes",
                    "items must be non-empty strings or objects",
                ));
            }
        }
    }
    Ok((events, bindings))
}

fn take_event_entries(
    values: &mut Map<String, Value>,
    field: &'static str,
    path: &Path,
) -> Result<Vec<Value>, SpecError> {
    let value = values.remove(field);
    let Some(value) = value else {
        return Ok(Vec::new());
    };
    if value == Value::Null {
        return Ok(Vec::new());
    }
    let Value::Array(value) = value else {
        return Err(invalid_field(path, field, "expected an array"));
    };
    Ok(value)
}

fn take_object(
    values: &mut Map<String, Value>,
    name: &str,
    parent: &'static str,
    path: &Path,
) -> Result<Map<String, Value>, SpecError> {
    match values.remove(name) {
        None => Ok(Map::new()),
        Some(Value::Object(value)) => Ok(value),
        Some(
            Value::Null | Value::Bool(_) | Value::Number(_) | Value::String(_) | Value::Array(_),
        ) => Err(invalid_field(
            path,
            parent,
            format!("{name} must be an object"),
        )),
    }
}

fn take_optional_string(
    values: &mut Map<String, Value>,
    name: &str,
    parent: &'static str,
    path: &Path,
) -> Result<Option<String>, SpecError> {
    match values.remove(name) {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(value)) if !value.is_empty() => Ok(Some(value)),
        Some(
            Value::Bool(_)
            | Value::Number(_)
            | Value::String(_)
            | Value::Array(_)
            | Value::Object(_),
        ) => Err(invalid_field(
            path,
            parent,
            format!("{name} must be a non-empty string"),
        )),
    }
}

fn take_schedules(
    values: &mut Map<String, Value>,
    path: &Path,
) -> Result<Vec<ScheduleEntry>, SpecError> {
    let value = values.remove("schedules");
    let Some(value) = value else {
        return Ok(Vec::new());
    };
    if value == Value::Null {
        return Ok(Vec::new());
    }
    let Value::Array(values) = value else {
        return Err(invalid_field(path, "schedules", "expected an array"));
    };
    let mut schedules = Vec::with_capacity(values.len());
    for value in values {
        let Value::Object(mut value) = value else {
            return Err(invalid_field(path, "schedules", "items must be objects"));
        };
        if value.contains_key("event") {
            return Err(invalid_field(path, "schedules", "event is not supported"));
        }
        if value.contains_key("payload") {
            return Err(invalid_field(path, "schedules", "payload is not supported"));
        }
        let cron = take_nested_required_string(&mut value, "cron", "schedules", path)?;
        let timezone = take_optional_string(&mut value, "timezone", "schedules", path)?;
        let catchup = take_optional_string(&mut value, "catchup", "schedules", path)?;
        if let Some(catchup) = &catchup {
            if catchup != "latest" {
                return Err(invalid_field(path, "schedules", "catchup must be latest"));
            }
        }
        schedules.push(ScheduleEntry {
            cron,
            timezone,
            catchup,
        });
    }
    Ok(schedules)
}

fn take_retry(
    values: &mut Map<String, Value>,
    path: &Path,
) -> Result<Option<RetrySpec>, SpecError> {
    let value = values.remove("retry");
    let Some(value) = value else {
        return Ok(None);
    };
    if value == Value::Null {
        return Ok(None);
    }
    let Value::Object(mut value) = value else {
        return Err(invalid_field(path, "retry", "expected an object"));
    };
    reject_unknown_fields(&value, &["max_attempts", "backoff_seconds"], "retry", path)?;
    let max_attempts = match value.remove("max_attempts") {
        None | Some(Value::Null) => None,
        Some(Value::Number(number)) => {
            let Some(number) = number.as_u64() else {
                return Err(invalid_field(
                    path,
                    "retry",
                    "max_attempts must be a positive integer",
                ));
            };
            if number == 0 {
                return Err(invalid_field(
                    path,
                    "retry",
                    "max_attempts must be a positive integer",
                ));
            }
            Some(number)
        }
        Some(Value::Bool(_) | Value::String(_) | Value::Array(_) | Value::Object(_)) => {
            return Err(invalid_field(
                path,
                "retry",
                "max_attempts must be a positive integer",
            ));
        }
    };
    let backoff_seconds = match value.remove("backoff_seconds") {
        None | Some(Value::Null) => None,
        Some(Value::Number(number)) => {
            let Some(number) = number.as_f64() else {
                return Err(invalid_field(
                    path,
                    "retry",
                    "backoff_seconds must be a non-negative number",
                ));
            };
            if !number.is_finite() || number < 0.0 {
                return Err(invalid_field(
                    path,
                    "retry",
                    "backoff_seconds must be a non-negative number",
                ));
            }
            Some(number)
        }
        Some(Value::Bool(_) | Value::String(_) | Value::Array(_) | Value::Object(_)) => {
            return Err(invalid_field(
                path,
                "retry",
                "backoff_seconds must be a non-negative number",
            ));
        }
    };
    Ok(Some(RetrySpec {
        max_attempts,
        backoff_seconds,
    }))
}

fn take_base_dir(
    values: &mut Map<String, Value>,
    path: &Path,
) -> Result<Option<PathBuf>, SpecError> {
    match values.remove("base_dir") {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(value)) => {
            if value.trim().is_empty() {
                return Err(invalid_field(path, "base_dir", "expected a path string"));
            }
            let base_dir = PathBuf::from(&value);
            if !base_dir.is_absolute() && value != "~" && !value.starts_with("~/") {
                return Err(invalid_field(
                    path,
                    "base_dir",
                    "expected an absolute or home-relative path",
                ));
            }
            Ok(Some(base_dir))
        }
        Some(Value::Bool(_) | Value::Number(_) | Value::Array(_) | Value::Object(_)) => {
            Err(invalid_field(path, "base_dir", "expected a path string"))
        }
    }
}

fn reject_unknown_fields(
    values: &Map<String, Value>,
    allowed: &[&str],
    field: &'static str,
    path: &Path,
) -> Result<(), SpecError> {
    for key in values.keys() {
        if !allowed.contains(&key.as_str()) {
            return Err(invalid_field(
                path,
                field,
                format!("unsupported field {key:?}"),
            ));
        }
    }
    Ok(())
}

fn invalid_field(path: &Path, field: &'static str, detail: impl Into<String>) -> SpecError {
    SpecError::new(SpecErrorKind::InvalidField, Some(field), path, detail)
}
