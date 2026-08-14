//! Loads and parses authored agent Markdown into declaration values.

use std::{
    collections::{BTreeMap, BTreeSet},
    fs,
    path::{Path, PathBuf},
};

use minijinja::{AutoEscape, Environment};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Number, Value};
use yaml_serde::Value as YamlValue;
use zeta_substrate::{hash_bytes, Hash, Object};

use crate::error::{AgentSpecError, ManifestError, ManifestErrorKind, SpecErrorKind};
use crate::spec::{
    scheduled_event_type, AgentSpec, EgressBinding, ExecutorSpec, IngressBinding, ModelSpec,
    RetrySpec, ScheduleEntry,
};

/// Loads one authored agent from a Markdown file.
///
/// The filename stem supplies the agent slug. The file is read exactly once,
/// and failures retain the supplied path for diagnostics.
///
/// # Errors
///
/// Returns [`AgentSpecError`] when the path cannot be read, its filename does not
/// produce a valid slug, or its contents are invalid.
///
/// [`AgentSpecError`]: crate::AgentSpecError
///
/// # Examples
///
/// ```no_run
/// use std::path::Path;
///
/// let spec = zeta_manifest::load_agent(Path::new("agents/worker.md"))?;
/// assert_eq!(spec.slug, "worker");
/// # Ok::<(), zeta_manifest::AgentSpecError>(())
/// ```
pub fn load_agent(path: &Path) -> Result<AgentSpec, AgentSpecError> {
    let source = fs::read(path).map_err(|error| {
        AgentSpecError::new(SpecErrorKind::Io, None, error.to_string()).with_path(path)
    })?;
    let slug = slug_from_path(path).map_err(|error| error.with_path(path))?;
    parse_agent(slug, &source).map_err(|error| error.with_path(path))
}

/// Parses one authored agent from exact UTF-8 Markdown bytes.
///
/// The function performs no filesystem access. `slug` supplies the agent
/// identity used by schedule events.
///
/// # Errors
///
/// Returns [`AgentSpecError`] when the slug, source, frontmatter, or declaration
/// values are invalid.
///
/// [`AgentSpecError`]: crate::AgentSpecError
///
/// # Examples
///
/// ```
/// let spec = zeta_manifest::parse_agent(
///     "worker",
///     b"---\nname: Worker\ndescription: Does work.\n---\nWork.\n",
/// )?;
/// assert_eq!(spec.instructions, "Work.\n");
/// # Ok::<(), zeta_manifest::AgentSpecError>(())
/// ```
pub fn parse_agent(slug: &str, source: &[u8]) -> Result<AgentSpec, AgentSpecError> {
    let slug = validate_slug(slug)?;
    let content = std::str::from_utf8(source).map_err(|error| {
        AgentSpecError::new(SpecErrorKind::InvalidUtf8, None, error.to_string())
    })?;
    let (frontmatter, instructions) = split_frontmatter(content)?;
    let mut frontmatter = parse_frontmatter(frontmatter)?;

    let skills_inherit = !frontmatter.contains_key("skills");
    let tools_inherit = !frontmatter.contains_key("tools");
    let name = take_required_string(&mut frontmatter, "name")?;
    let description = take_required_string(&mut frontmatter, "description")?;
    let enabled = take_bool(&mut frontmatter, "enabled", true)?;
    let session = take_session(&mut frontmatter)?;
    let model = take_model(&mut frontmatter)?;
    let executor = take_executor(&mut frontmatter)?;
    let (mut accepts, ingress) = take_accepts(&mut frontmatter)?;
    let (publishes, egress) = take_publishes(&mut frontmatter)?;
    let returns = take_string_list(&mut frontmatter, "returns")?;
    let skills = take_string_list(&mut frontmatter, "skills")?;
    let tools = take_string_list(&mut frontmatter, "tools")?;
    let schedules = take_schedules(&mut frontmatter)?;
    let retry = take_retry(&mut frontmatter)?;
    let base_dir = take_base_dir(&mut frontmatter)?;
    let locks = take_locks(&mut frontmatter)?;

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
        source: content.to_owned(),
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
        locks,
        extensions: frontmatter,
    })
}

/// Stores one flat authored skill with its content identity.
///
/// The supplied bytes must be UTF-8 and are retained exactly as a string. The
/// identity uses a `skill` substrate object with the `zeta.skill.v1` schema.
///
/// # Examples
///
/// ```
/// let skill = zeta_manifest::SkillResource::new(
///     "code-review",
///     b"Review for correctness.\n",
/// )?;
/// assert_eq!(skill.name, "code-review");
/// assert!(skill.object_id.to_string().starts_with("b3:"));
/// # Ok::<(), zeta_manifest::ManifestError>(())
/// ```
#[derive(Clone, Debug, Deserialize, PartialEq, Eq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct SkillResource {
    /// Names the resource within its authored project.
    pub name: String,
    /// Preserves the exact UTF-8 source text.
    pub body: String,
    /// Identifies the `zeta.skill.v1` substrate object.
    pub object_id: Hash,
}

impl SkillResource {
    /// Creates a flat skill resource from a supplied name and exact bytes.
    ///
    /// # Errors
    ///
    /// Returns [`ManifestError`] when `source` is not valid UTF-8.
    ///
    /// [`ManifestError`]: crate::ManifestError
    ///
    /// # Examples
    ///
    /// ```
    /// let skill = zeta_manifest::SkillResource::new("review", b"Review.\n")?;
    /// assert_eq!(skill.body, "Review.\n");
    /// # Ok::<(), zeta_manifest::ManifestError>(())
    /// ```
    pub fn new(name: &str, source: &[u8]) -> Result<Self, ManifestError> {
        let body = std::str::from_utf8(source).map_err(|error| {
            ManifestError::new(
                ManifestErrorKind::InvalidSkill,
                Some(name),
                Some("body"),
                format!("skill body is not valid UTF-8: {error}"),
            )
        })?;
        let mut data = Map::new();
        data.insert("body".to_owned(), Value::String(body.to_owned()));
        let object = Object {
            kind: "skill".to_owned(),
            schema: "zeta.skill.v1".to_owned(),
            data,
            links: Vec::new(),
        };
        let object_id = object
            .content_address()
            .expect("a skill body contains no fallible canonical JSON numbers");
        Ok(SkillResource {
            name: name.to_owned(),
            body: body.to_owned(),
            object_id,
        })
    }
}

/// Describes metadata and body parsed from supplied `SKILL.md` bytes.
///
/// Unlike [`SkillResource`], this declaration follows the `SKILL.md`
/// frontmatter convention and carries model-invocation metadata.
///
/// [`SkillResource`]: crate::SkillResource
///
/// # Examples
///
/// ```
/// let skill = zeta_manifest::parse_skill(
///     "review",
///     b"---\ndescription: Reviews changes.\n---\nReview.\n",
/// )?;
/// assert_eq!(skill.name, "review");
/// # Ok::<(), zeta_manifest::ManifestError>(())
/// ```
#[derive(Clone, Debug, Deserialize, PartialEq, Eq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct SkillSpec {
    /// Names the skill using lowercase letters, digits, and hyphens.
    pub name: String,
    /// Describes when the skill applies.
    pub description: String,
    /// Preserves the authored body after frontmatter.
    pub body: String,
    /// Prevents the skill from being advertised for model invocation.
    pub disable_model_invocation: bool,
}

/// Validates one agent's authored prompt template.
///
/// Templates can read `event` and can introduce local variables. MiniJinja's
/// built-in globals remain available, while all other undeclared roots are
/// rejected.
///
/// # Errors
///
/// Returns [`ManifestError`] when the template has invalid syntax or refers
/// to an undeclared root other than `event`.
///
/// [`ManifestError`]: crate::ManifestError
///
/// # Examples
///
/// ```
/// let spec = zeta_manifest::parse_agent(
///     "worker",
///     b"---\nname: Worker\ndescription: Works.\n---\n{{ event.payload.text }}\n",
/// )?;
/// zeta_manifest::validate_prompt(&spec).unwrap();
/// # Ok::<(), zeta_manifest::AgentSpecError>(())
/// ```
pub fn validate_prompt(spec: &AgentSpec) -> Result<(), ManifestError> {
    let environment = prompt_environment();
    let template = environment
        .template_from_str(&spec.instructions)
        .map_err(|error| prompt_error(spec, ManifestErrorKind::InvalidPromptSyntax, error))?;
    let globals = BTreeSet::from(["dict", "namespace", "range"]);
    let mut roots = Vec::new();
    for root in template.undeclared_variables(false) {
        if root != "event" && !globals.contains(root.as_str()) {
            roots.push(root);
        }
    }
    roots.sort();
    let Some(root) = roots.first() else {
        return Ok(());
    };
    Err(ManifestError::new(
        ManifestErrorKind::UnknownPromptRoot,
        Some(&spec.slug),
        Some("instructions"),
        format!("template references unknown variable {root:?}"),
    ))
}

/// Renders one agent prompt against a supplied event value.
///
/// Rendering performs no autoescaping. Missing terminal members render as an
/// empty string, following the portable lenient undefined-value contract.
///
/// # Errors
///
/// Returns [`ManifestError`] when the template has invalid syntax or fails at
/// runtime, such as when it traverses through an undefined intermediate value.
///
/// [`ManifestError`]: crate::ManifestError
///
/// # Examples
///
/// ```
/// let spec = zeta_manifest::parse_agent(
///     "worker",
///     b"---\nname: Worker\ndescription: Works.\n---\n{{ event.payload.text }}",
/// )?;
/// let event = serde_json::json!({"payload": {"text": "hello"}});
/// assert_eq!(zeta_manifest::render_prompt(&spec, &event).unwrap(), "hello");
/// # Ok::<(), zeta_manifest::AgentSpecError>(())
/// ```
pub fn render_prompt(spec: &AgentSpec, event: &Value) -> Result<String, ManifestError> {
    let environment = prompt_environment();
    let template = environment
        .template_from_str(&spec.instructions)
        .map_err(|error| prompt_error(spec, ManifestErrorKind::InvalidPromptSyntax, error))?;
    template
        .render(minijinja::context! { event => event })
        .map_err(|error| prompt_error(spec, ManifestErrorKind::PromptRender, error))
}

/// Parses one `SKILL.md` declaration from supplied exact bytes.
///
/// `fallback_name` is used when frontmatter does not provide a non-empty name.
/// The function performs no filesystem access or skill discovery.
///
/// # Errors
///
/// Returns [`ManifestError`] when the bytes are not UTF-8, the resolved name
/// is invalid, or the description is absent or empty.
///
/// [`ManifestError`]: crate::ManifestError
///
/// # Examples
///
/// ```
/// let skill = zeta_manifest::parse_skill(
///     "review",
///     b"---\ndescription: Reviews changes.\n---\nReview.\n",
/// )?;
/// assert_eq!(skill.body, "Review.\n");
/// # Ok::<(), zeta_manifest::ManifestError>(())
/// ```
pub fn parse_skill(fallback_name: &str, source: &[u8]) -> Result<SkillSpec, ManifestError> {
    let source = std::str::from_utf8(source).map_err(|error| {
        ManifestError::new(
            ManifestErrorKind::InvalidSkill,
            Some(fallback_name),
            Some("body"),
            format!("SKILL.md is not valid UTF-8: {error}"),
        )
    })?;
    let (metadata, body) = split_skill_frontmatter(source);
    let metadata = parse_skill_metadata(metadata);
    let name = skill_metadata_text(metadata.get("name"), fallback_name);
    let name = name.trim();
    if !valid_skill_name(name) {
        return Err(ManifestError::new(
            ManifestErrorKind::InvalidSkill,
            Some(name),
            Some("name"),
            format!("invalid skill name {name:?}: use lowercase letters, digits, and hyphens"),
        ));
    }
    let description = skill_metadata_text(metadata.get("description"), "");
    let description = description.trim();
    if description.is_empty() {
        return Err(ManifestError::new(
            ManifestErrorKind::InvalidSkill,
            Some(name),
            Some("description"),
            "missing non-empty description",
        ));
    }
    let disable_model_invocation = skill_metadata_bool(metadata.get("disable-model-invocation"));
    Ok(SkillSpec {
        name: name.to_owned(),
        description: description.to_owned(),
        body: body.to_owned(),
        disable_model_invocation,
    })
}

fn prompt_environment<'source>() -> Environment<'source> {
    let mut environment = Environment::new();
    environment.set_auto_escape_callback(|_name| AutoEscape::None);
    environment
}

fn prompt_error(
    spec: &AgentSpec,
    kind: ManifestErrorKind,
    error: minijinja::Error,
) -> ManifestError {
    ManifestError::new(
        kind,
        Some(&spec.slug),
        Some("instructions"),
        error.to_string(),
    )
}

#[derive(Clone, Debug, PartialEq, Eq)]
enum SkillMetadataValue {
    Bool(bool),
    String(String),
}

fn split_skill_frontmatter(content: &str) -> (&str, &str) {
    let mut lines = content.split_inclusive('\n');
    let Some(first) = lines.next() else {
        return ("", content);
    };
    if first.trim() != "---" {
        return ("", content);
    }
    let metadata_start = first.len();
    let mut line_start = metadata_start;
    for line in lines {
        if line.trim() == "---" {
            let metadata = &content[metadata_start..line_start];
            let body = &content[line_start + line.len()..];
            return (metadata, body);
        }
        line_start += line.len();
    }
    ("", content)
}

fn parse_skill_metadata(source: &str) -> BTreeMap<String, SkillMetadataValue> {
    let mut metadata = BTreeMap::new();
    for line in source.lines() {
        let line = line.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let Some((key, value)) = line.split_once(':') else {
            continue;
        };
        let key = key.trim();
        if key.is_empty() {
            continue;
        }
        metadata.insert(key.to_owned(), parse_skill_scalar(value.trim()));
    }
    metadata
}

fn parse_skill_scalar(value: &str) -> SkillMetadataValue {
    if value.eq_ignore_ascii_case("true") {
        return SkillMetadataValue::Bool(true);
    }
    if value.eq_ignore_ascii_case("false") {
        return SkillMetadataValue::Bool(false);
    }
    let bytes = value.as_bytes();
    if bytes.len() >= 2 {
        let quote = bytes[0];
        if (quote == b'\'' || quote == b'"') && bytes[bytes.len() - 1] == quote {
            return SkillMetadataValue::String(value[1..value.len() - 1].to_owned());
        }
    }
    SkillMetadataValue::String(value.to_owned())
}

fn skill_metadata_text<'a>(value: Option<&'a SkillMetadataValue>, fallback: &'a str) -> &'a str {
    match value {
        None | Some(SkillMetadataValue::Bool(false)) => fallback,
        Some(SkillMetadataValue::Bool(true)) => "True",
        Some(SkillMetadataValue::String(value)) if value.is_empty() => fallback,
        Some(SkillMetadataValue::String(value)) => value,
    }
}

fn skill_metadata_bool(value: Option<&SkillMetadataValue>) -> bool {
    match value {
        None | Some(SkillMetadataValue::Bool(false)) => false,
        Some(SkillMetadataValue::Bool(true)) => true,
        Some(SkillMetadataValue::String(value)) => value.trim().eq_ignore_ascii_case("true"),
    }
}

fn valid_skill_name(name: &str) -> bool {
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

fn split_frontmatter(content: &str) -> Result<(&str, &str), AgentSpecError> {
    let mut lines = content.split_inclusive('\n');
    let Some(first) = lines.next() else {
        return Err(AgentSpecError::new(
            SpecErrorKind::MissingFrontmatterDelimiter,
            None,
            "the first line must be ---",
        ));
    };
    if first.trim() != "---" {
        return Err(AgentSpecError::new(
            SpecErrorKind::MissingFrontmatterDelimiter,
            None,
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
    Err(AgentSpecError::new(
        SpecErrorKind::MissingClosingFrontmatterDelimiter,
        None,
        "frontmatter must end with ---",
    ))
}

fn parse_frontmatter(source: &str) -> Result<Map<String, Value>, AgentSpecError> {
    let source = yaml_serde::from_str::<YamlValue>(source).map_err(|error| {
        AgentSpecError::new(SpecErrorKind::InvalidYaml, None, error.to_string())
    })?;
    let mapping = match source {
        YamlValue::Null => return Ok(Map::new()),
        YamlValue::Mapping(mapping) => mapping,
        YamlValue::Bool(_)
        | YamlValue::Number(_)
        | YamlValue::String(_)
        | YamlValue::Sequence(_)
        | YamlValue::Tagged(_) => {
            return Err(AgentSpecError::new(
                SpecErrorKind::ExpectedFrontmatterObject,
                None,
                "frontmatter must be an object",
            ));
        }
    };

    let mut output = Map::new();
    for (key, value) in mapping {
        let YamlValue::String(key) = key else {
            return Err(AgentSpecError::new(
                SpecErrorKind::ExpectedFrontmatterObject,
                None,
                "frontmatter keys must be strings",
            ));
        };
        if key == "<<" {
            return Err(AgentSpecError::new(
                SpecErrorKind::InvalidField,
                Some(&key),
                "merge keys are not supported",
            ));
        }
        let value = yaml_to_json(value).map_err(|detail| {
            AgentSpecError::new(SpecErrorKind::InvalidField, Some(&key), detail)
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

fn slug_from_path(path: &Path) -> Result<&str, AgentSpecError> {
    let Some(stem) = path.file_stem().and_then(|stem| stem.to_str()) else {
        return Err(AgentSpecError::new(
            SpecErrorKind::InvalidSlug,
            None,
            "the filename must have a UTF-8 stem",
        ));
    };
    Ok(stem)
}

fn validate_slug(slug: &str) -> Result<String, AgentSpecError> {
    if slug.is_empty() {
        return Err(AgentSpecError::new(
            SpecErrorKind::InvalidSlug,
            None,
            "the slug must match [a-z0-9_-]+",
        ));
    }
    for byte in slug.bytes() {
        let valid =
            byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'_' || byte == b'-';
        if !valid {
            return Err(AgentSpecError::new(
                SpecErrorKind::InvalidSlug,
                None,
                "the slug must match [a-z0-9_-]+",
            ));
        }
    }
    Ok(slug.to_owned())
}

fn take_required_string(
    values: &mut Map<String, Value>,
    field: &'static str,
) -> Result<String, AgentSpecError> {
    let value = values.remove(field);
    let Some(Value::String(value)) = value else {
        return Err(AgentSpecError::new(
            SpecErrorKind::MissingRequiredField,
            Some(field),
            "a non-empty string is required",
        ));
    };
    if value.is_empty() {
        return Err(AgentSpecError::new(
            SpecErrorKind::MissingRequiredField,
            Some(field),
            "a non-empty string is required",
        ));
    }
    Ok(value)
}

fn take_bool(
    values: &mut Map<String, Value>,
    field: &'static str,
    default: bool,
) -> Result<bool, AgentSpecError> {
    match values.remove(field) {
        None => Ok(default),
        Some(Value::Bool(value)) => Ok(value),
        Some(
            Value::Null | Value::Number(_) | Value::String(_) | Value::Array(_) | Value::Object(_),
        ) => Err(invalid_field(field, "expected a boolean")),
    }
}

fn take_session(values: &mut Map<String, Value>) -> Result<String, AgentSpecError> {
    match values.remove("session") {
        None | Some(Value::Null) => Ok("per-event".to_owned()),
        Some(Value::String(value)) => {
            if value == "shared" || value == "per-event" || value.contains('{') {
                Ok(value)
            } else {
                Err(invalid_field(
                    "session",
                    "expected shared, per-event, or a template",
                ))
            }
        }
        Some(Value::Bool(_) | Value::Number(_) | Value::Array(_) | Value::Object(_)) => {
            Err(invalid_field("session", "expected a string"))
        }
    }
}

fn take_model(values: &mut Map<String, Value>) -> Result<Option<ModelSpec>, AgentSpecError> {
    let value = values.remove("model");
    let Some(value) = value else {
        return Ok(None);
    };
    if value == Value::Null {
        return Ok(None);
    }
    match value {
        Value::String(profile) => {
            if profile.trim().is_empty() {
                return Err(invalid_field("model", "must not be empty"));
            }
            Ok(Some(ModelSpec::Profile(profile)))
        }
        Value::Object(mut value) => {
            reject_unknown_fields(&value, &["name", "url"], "model")?;
            let name = take_nested_required_string(&mut value, "name", "model")?;
            let url = take_nested_required_string(&mut value, "url", "model")?;
            Ok(Some(ModelSpec::Endpoint { name, url }))
        }
        Value::Bool(_) | Value::Number(_) | Value::Array(_) | Value::Null => {
            Err(invalid_field("model", "expected a profile name string"))
        }
    }
}

fn take_executor(values: &mut Map<String, Value>) -> Result<ExecutorSpec, AgentSpecError> {
    let value = values.remove("executor");
    let Some(value) = value else {
        return Ok(ExecutorSpec::default());
    };
    if value == Value::Null {
        return Ok(ExecutorSpec::default());
    }
    let Value::Object(mut value) = value else {
        return Err(invalid_field("executor", "expected an object"));
    };
    reject_unknown_fields(&value, &["provider", "config"], "executor")?;
    let provider = take_nested_required_string(&mut value, "provider", "executor")?;
    let config = match value.remove("config") {
        None => Map::new(),
        Some(Value::Object(config)) => config,
        Some(
            Value::Null | Value::Bool(_) | Value::Number(_) | Value::String(_) | Value::Array(_),
        ) => return Err(invalid_field("executor", "config must be an object")),
    };
    Ok(ExecutorSpec { provider, config })
}

fn take_nested_required_string(
    values: &mut Map<String, Value>,
    name: &str,
    parent: &'static str,
) -> Result<String, AgentSpecError> {
    let value = values.remove(name);
    let Some(Value::String(value)) = value else {
        return Err(invalid_field(
            parent,
            format!("{name} must be a non-empty string"),
        ));
    };
    if value.is_empty() {
        return Err(invalid_field(
            parent,
            format!("{name} must be a non-empty string"),
        ));
    }
    Ok(value)
}

fn take_string_list(
    values: &mut Map<String, Value>,
    field: &'static str,
) -> Result<Vec<String>, AgentSpecError> {
    let value = values.remove(field);
    let Some(value) = value else {
        return Ok(Vec::new());
    };
    if value == Value::Null {
        return Ok(Vec::new());
    }
    let Value::Array(values) = value else {
        return Err(invalid_field(field, "expected an array"));
    };
    let mut output = Vec::with_capacity(values.len());
    for value in values {
        let Value::String(value) = value else {
            return Err(invalid_field(field, "items must be non-empty strings"));
        };
        if value.is_empty() {
            return Err(invalid_field(field, "items must be non-empty strings"));
        }
        output.push(value);
    }
    Ok(output)
}

fn take_accepts(
    values: &mut Map<String, Value>,
) -> Result<(Vec<String>, Vec<IngressBinding>), AgentSpecError> {
    let entries = take_event_entries(values, "accepts")?;
    let mut events = Vec::with_capacity(entries.len());
    let mut bindings = Vec::new();
    for entry in entries {
        match entry {
            Value::String(event) if !event.is_empty() => events.push(event),
            Value::Object(mut entry) => {
                reject_unknown_fields(&entry, &["event", "filter", "idempotency_key"], "accepts")?;
                let event = take_nested_required_string(&mut entry, "event", "accepts")?;
                let filter = take_object(&mut entry, "filter", "accepts")?;
                let idempotency_key =
                    take_optional_string(&mut entry, "idempotency_key", "accepts")?;
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
) -> Result<(Vec<String>, Vec<EgressBinding>), AgentSpecError> {
    let entries = take_event_entries(values, "publishes")?;
    let mut events = Vec::with_capacity(entries.len());
    let mut bindings = Vec::new();
    for entry in entries {
        match entry {
            Value::String(event) if !event.is_empty() => events.push(event),
            Value::Object(mut entry) => {
                if entry.contains_key("filter") {
                    return Err(invalid_field(
                        "publishes",
                        "published event options use 'with'",
                    ));
                }
                reject_unknown_fields(&entry, &["event", "with", "idempotency_key"], "publishes")?;
                let event = take_nested_required_string(&mut entry, "event", "publishes")?;
                let options = take_object(&mut entry, "with", "publishes")?;
                let idempotency_key =
                    take_optional_string(&mut entry, "idempotency_key", "publishes")?;
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
) -> Result<Vec<Value>, AgentSpecError> {
    let value = values.remove(field);
    let Some(value) = value else {
        return Ok(Vec::new());
    };
    if value == Value::Null {
        return Ok(Vec::new());
    }
    let Value::Array(value) = value else {
        return Err(invalid_field(field, "expected an array"));
    };
    Ok(value)
}

fn take_object(
    values: &mut Map<String, Value>,
    name: &str,
    parent: &'static str,
) -> Result<Map<String, Value>, AgentSpecError> {
    match values.remove(name) {
        None => Ok(Map::new()),
        Some(Value::Object(value)) => Ok(value),
        Some(
            Value::Null | Value::Bool(_) | Value::Number(_) | Value::String(_) | Value::Array(_),
        ) => Err(invalid_field(parent, format!("{name} must be an object"))),
    }
}

fn take_optional_string(
    values: &mut Map<String, Value>,
    name: &str,
    parent: &'static str,
) -> Result<Option<String>, AgentSpecError> {
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
            parent,
            format!("{name} must be a non-empty string"),
        )),
    }
}

fn take_schedules(values: &mut Map<String, Value>) -> Result<Vec<ScheduleEntry>, AgentSpecError> {
    let value = values.remove("schedules");
    let Some(value) = value else {
        return Ok(Vec::new());
    };
    if value == Value::Null {
        return Ok(Vec::new());
    }
    let Value::Array(values) = value else {
        return Err(invalid_field("schedules", "expected an array"));
    };
    let mut schedules = Vec::with_capacity(values.len());
    for value in values {
        let Value::Object(mut value) = value else {
            return Err(invalid_field("schedules", "items must be objects"));
        };
        if value.contains_key("event") {
            return Err(invalid_field("schedules", "event is not supported"));
        }
        if value.contains_key("payload") {
            return Err(invalid_field("schedules", "payload is not supported"));
        }
        let cron = take_nested_required_string(&mut value, "cron", "schedules")?;
        let timezone = take_optional_string(&mut value, "timezone", "schedules")?;
        let catchup = take_optional_string(&mut value, "catchup", "schedules")?;
        if let Some(catchup) = &catchup {
            if catchup != "latest" {
                return Err(invalid_field("schedules", "catchup must be latest"));
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

fn take_retry(values: &mut Map<String, Value>) -> Result<Option<RetrySpec>, AgentSpecError> {
    let value = values.remove("retry");
    let Some(value) = value else {
        return Ok(None);
    };
    if value == Value::Null {
        return Ok(None);
    }
    let Value::Object(mut value) = value else {
        return Err(invalid_field("retry", "expected an object"));
    };
    reject_unknown_fields(&value, &["max_attempts", "backoff_seconds"], "retry")?;
    let max_attempts = match value.remove("max_attempts") {
        None | Some(Value::Null) => None,
        Some(Value::Number(number)) => {
            let Some(number) = number.as_u64() else {
                return Err(invalid_field(
                    "retry",
                    "max_attempts must be a positive integer",
                ));
            };
            if number == 0 {
                return Err(invalid_field(
                    "retry",
                    "max_attempts must be a positive integer",
                ));
            }
            Some(number)
        }
        Some(Value::Bool(_) | Value::String(_) | Value::Array(_) | Value::Object(_)) => {
            return Err(invalid_field(
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
                    "retry",
                    "backoff_seconds must be a non-negative number",
                ));
            };
            if !number.is_finite() || number < 0.0 {
                return Err(invalid_field(
                    "retry",
                    "backoff_seconds must be a non-negative number",
                ));
            }
            Some(number)
        }
        Some(Value::Bool(_) | Value::String(_) | Value::Array(_) | Value::Object(_)) => {
            return Err(invalid_field(
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

fn take_base_dir(values: &mut Map<String, Value>) -> Result<Option<PathBuf>, AgentSpecError> {
    match values.remove("base_dir") {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(value)) => {
            if value.trim().is_empty() {
                return Err(invalid_field("base_dir", "expected a path string"));
            }
            Ok(Some(PathBuf::from(value)))
        }
        Some(Value::Bool(_) | Value::Number(_) | Value::Array(_) | Value::Object(_)) => {
            Err(invalid_field("base_dir", "expected a path string"))
        }
    }
}

fn take_locks(values: &mut Map<String, Value>) -> Result<Vec<String>, AgentSpecError> {
    let Some(value) = values.remove("locks") else {
        return Ok(Vec::new());
    };
    if value.is_null() {
        return Ok(Vec::new());
    }
    if let Some(value) = value.as_str() {
        if value.is_empty() {
            return Err(invalid_field("locks", "lock identities must be non-empty"));
        }
        return Ok(vec![value.to_owned()]);
    }
    let Some(values) = value.as_array() else {
        return Err(invalid_field(
            "locks",
            "expected a string or list of strings",
        ));
    };
    let mut locks = Vec::new();
    for value in values {
        let Some(value) = value.as_str() else {
            return Err(invalid_field("locks", "expected a list of strings"));
        };
        if value.is_empty() {
            return Err(invalid_field("locks", "lock identities must be non-empty"));
        }
        if !locks.contains(&value.to_owned()) {
            locks.push(value.to_owned());
        }
    }
    Ok(locks)
}

fn reject_unknown_fields(
    values: &Map<String, Value>,
    allowed: &[&str],
    field: &'static str,
) -> Result<(), AgentSpecError> {
    for key in values.keys() {
        if !allowed.contains(&key.as_str()) {
            return Err(invalid_field(field, format!("unsupported field {key:?}")));
        }
    }
    Ok(())
}

fn invalid_field(field: &'static str, detail: impl Into<String>) -> AgentSpecError {
    AgentSpecError::new(SpecErrorKind::InvalidField, Some(field), detail)
}
