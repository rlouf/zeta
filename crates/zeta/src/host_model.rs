//! Resolves the host-owned model selection for native agent runs.

use std::collections::{BTreeMap, BTreeSet};
use std::env;
use std::fs;
use std::path::{Path, PathBuf};
use std::time::Duration;

use base64::Engine as _;
use serde_json::{Map, Value, json};
use zeta_agent::{
    AbortSignal, AgentObserver, HttpModelGateway, HttpModelGatewayConfig, ModelGateway,
    ModelHttpEndpoint, ModelInput, ModelRequest, ModelTransportTimeouts, Observation, ToolProfile,
};
use zeta_manifest::ModelSpec;

use crate::ProjectRevision;

const CODEX_API: &str = "codex-responses";
const CHAT_API: &str = "chat-completions";
const DEFAULT_CODEX_URL: &str = "https://chatgpt.com/backend-api/codex/responses";
const PROJECT_CONFIG_FILE: &str = "zeta.toml";
const VERIFY_FIRST_OUTPUT_TIMEOUT: Duration = Duration::from_secs(10);
const VERIFY_IDLE_TIMEOUT: Duration = Duration::from_secs(5);
const VERIFY_TOTAL_TIMEOUT: Duration = Duration::from_secs(15);

/// Carries one host-resolved model request selection.
#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct ModelSelection {
    pub name: String,
    pub url: String,
    pub api: String,
    pub thinking: Option<String>,
    pub tool_profile: ToolProfile,
    pub headers: BTreeMap<String, String>,
}

#[derive(Clone, Debug, Default, Eq, PartialEq)]
struct ModelProfile {
    name: Option<String>,
    model: Option<String>,
    url: Option<String>,
    thinking: Option<String>,
    api: Option<String>,
    default: bool,
    tool_profile: Option<String>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum ProfileSource {
    Project,
    Local,
}

#[derive(Clone, Debug)]
struct ResolvedProfile {
    profile: ModelProfile,
    source: ProfileSource,
}

/// Carries a selected model and the profile source that supplied it.
#[derive(Clone, Debug)]
pub(crate) struct ResolvedModel {
    pub selection: ModelSelection,
    source: ProfileSource,
}

/// Resolves an agent model profile or the host default profile.
pub(crate) fn resolve(
    agent: Option<&ModelSpec>,
    project_root: &Path,
    session: &str,
) -> Result<ResolvedModel, String> {
    let resolved = match agent {
        Some(profile) => named_profile(profile.profile(), project_root)?,
        None => default_profile(project_root)?,
    };
    let mut selected = selection_from_profile(&resolved.profile)?;
    if selected.api == CODEX_API {
        selected.headers = codex_headers(session)?;
    }
    Ok(ResolvedModel {
        selection: selected,
        source: resolved.source,
    })
}

/// Validates every enabled agent model before the runtime starts.
pub(crate) async fn check_project(revision: &ProjectRevision) -> Result<Vec<String>, String> {
    let mut warnings = BTreeSet::new();
    let mut selections = BTreeMap::new();
    for agent in revision.agents().filter(|agent| agent.enabled) {
        let resolved =
            resolve(agent.model.as_ref(), revision.project_root(), "zeta-up").map_err(|error| {
                format!("cannot resolve a model for agent {:?}: {error}", agent.slug)
            })?;
        let selection = resolved.selection;
        selections
            .entry(selection_key(&selection))
            .or_insert(selection);
        if agent.model.is_some() && resolved.source == ProfileSource::Local {
            warnings.insert(format!(
                "agent {:?} uses local model profile {:?}; add it to {}",
                agent.slug,
                agent
                    .model
                    .as_ref()
                    .map(ModelSpec::profile)
                    .unwrap_or_default(),
                project_config_path(revision.project_root()).display(),
            ));
        }
    }
    for selection in selections.values() {
        verify(selection).await?;
    }
    Ok(warnings.into_iter().collect())
}

fn selection_key(selection: &ModelSelection) -> String {
    format!(
        "{}\u{0}{}\u{0}{}\u{0}{}\u{0}{:?}",
        selection.name,
        selection.url,
        selection.api,
        selection.thinking.as_deref().unwrap_or_default(),
        selection.tool_profile,
    )
}

async fn verify(selection: &ModelSelection) -> Result<(), String> {
    let endpoint = selection.headers.iter().fold(
        ModelHttpEndpoint::new(&selection.url),
        |endpoint, (name, value)| endpoint.with_header(name.clone(), value.clone()),
    );
    let (chat_completions, responses) = match selection.api.as_str() {
        CHAT_API => (Some(endpoint), None),
        CODEX_API => (None, Some(endpoint)),
        _ => return Err(format!("model {:?} has an unsupported API", selection.name)),
    };
    let config = HttpModelGatewayConfig::new(chat_completions, responses).with_timeouts(
        ModelTransportTimeouts::new(
            VERIFY_FIRST_OUTPUT_TIMEOUT,
            VERIFY_IDLE_TIMEOUT,
            VERIFY_TOTAL_TIMEOUT,
        ),
    );
    let mut gateway = HttpModelGateway::new(config)
        .map_err(|error| format!("cannot prepare model {:?}: {error}", selection.name))?;
    let input = ModelInput {
        messages: vec![model_message("Reply with ready.")],
        tools: Vec::new(),
        tool_choice: json!("none"),
        max_tokens: 16,
        selected_model: Some(selection.name.clone()),
        session_id: Some("zeta-up".to_owned()),
        thinking: selection.thinking.clone(),
    };
    let request = ModelRequest {
        api: Some(selection.api.clone()),
        model: Some(selection.name.clone()),
        url: Some(selection.url.clone()),
        thinking: selection.thinking.clone(),
        session_id: Some("zeta-up".to_owned()),
    };
    let mut observer = VerificationObserver;
    let abort = VerificationAbort;
    gateway
        .generate(&input, &request, &mut observer, &abort)
        .await
        .map_err(|error| format!("model {:?} verification failed: {error}", selection.name))?;
    Ok(())
}

fn model_message(content: &str) -> Map<String, Value> {
    json!({"role": "user", "content": content})
        .as_object()
        .expect("model verification message must be an object")
        .clone()
}

struct VerificationObserver;

impl AgentObserver for VerificationObserver {
    fn observe(&mut self, _observation: Observation) {}
}

struct VerificationAbort;

impl AbortSignal for VerificationAbort {
    fn reason(&self) -> Option<zeta_agent::AbortReason> {
        None
    }
}

fn named_profile(name: &str, project_root: &Path) -> Result<ResolvedProfile, String> {
    let project_path = project_config_path(project_root);
    let project = configured_profiles(&project_path)?;
    if let Some(profile) = find_profile(project, Some(name)) {
        return Ok(ResolvedProfile {
            profile,
            source: ProfileSource::Project,
        });
    }
    let local_path = model_config_path()?;
    let local = configured_profiles(&local_path)?;
    if let Some(profile) = find_profile(local, Some(name)) {
        return Ok(ResolvedProfile {
            profile,
            source: ProfileSource::Local,
        });
    }
    Err(format!(
        "model profile {name:?} is not configured in {} or {}",
        project_path.display(),
        local_path.display(),
    ))
}

fn default_profile(project_root: &Path) -> Result<ResolvedProfile, String> {
    let project_path = project_config_path(project_root);
    let project = configured_profiles(&project_path)?;
    if let Some(profile) = find_profile(project, None) {
        return Ok(ResolvedProfile {
            profile,
            source: ProfileSource::Project,
        });
    }
    let local_path = model_config_path()?;
    let local = configured_profiles(&local_path)?;
    if let Some(profile) = find_profile(local, None) {
        return Ok(ResolvedProfile {
            profile,
            source: ProfileSource::Local,
        });
    }
    Err("no default model profile is configured; model setup is not available yet".to_owned())
}

fn find_profile(profiles: Vec<ModelProfile>, requested: Option<&str>) -> Option<ModelProfile> {
    profiles.into_iter().find(|profile| match requested {
        Some(name) => profile.name.as_deref() == Some(name),
        None => profile.default,
    })
}

#[cfg(test)]
fn select_profile(
    requested: Option<&str>,
    project: Vec<ModelProfile>,
    local: Vec<ModelProfile>,
    project_path: &Path,
    local_path: &Path,
) -> Result<ResolvedProfile, String> {
    if let Some(profile) = find_profile(project, requested) {
        return Ok(ResolvedProfile {
            profile,
            source: ProfileSource::Project,
        });
    }
    if let Some(profile) = find_profile(local, requested) {
        return Ok(ResolvedProfile {
            profile,
            source: ProfileSource::Local,
        });
    }
    match requested {
        Some(name) => Err(format!(
            "model profile {name:?} is not configured in {} or {}",
            project_path.display(),
            local_path.display(),
        )),
        None => Err(
            "no default model profile is configured; model setup is not available yet".to_owned(),
        ),
    }
}

fn configured_profiles(path: &Path) -> Result<Vec<ModelProfile>, String> {
    let profiles = match fs::read_to_string(&path) {
        Ok(source) => parse_profiles(&source)
            .map_err(|error| format!("could not read {}: {error}", path.display()))?,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Vec::new(),
        Err(error) => return Err(format!("could not read {}: {error}", path.display())),
    };
    Ok(profiles)
}

fn project_config_path(project_root: &Path) -> PathBuf {
    project_root.join(PROJECT_CONFIG_FILE)
}

fn model_config_path() -> Result<PathBuf, String> {
    let Some(home) = env::var_os("HOME") else {
        return Err("HOME is not set".to_owned());
    };
    Ok(Path::new(&home).join(".zeta/models.toml"))
}

fn selection_from_profile(profile: &ModelProfile) -> Result<ModelSelection, String> {
    let name = required_profile_field(profile.name.as_deref(), "name")?;
    let model = required_profile_field(profile.model.as_deref(), "model")?;
    let api = profile.api.as_deref().unwrap_or(CHAT_API);
    if api != CHAT_API && api != CODEX_API {
        return Err(format!(
            "model profile {name:?} has unsupported API {api:?}"
        ));
    }
    let url = match profile.url.as_deref() {
        Some(url) if !url.trim().is_empty() => url.to_owned(),
        Some(_) => return Err(format!("model profile {name:?} has an empty URL")),
        None if api == CODEX_API => DEFAULT_CODEX_URL.to_owned(),
        None => "http://127.0.0.1:8080/v1/chat/completions".to_owned(),
    };
    let tool_profile = match profile.tool_profile.as_deref().unwrap_or("native") {
        "native" => ToolProfile::Native,
        "codex" => ToolProfile::Codex,
        value => {
            return Err(format!(
                "model profile {name:?} has unknown tool profile {value:?}"
            ));
        }
    };
    Ok(ModelSelection {
        name: model.to_owned(),
        url,
        api: api.to_owned(),
        thinking: profile.thinking.clone(),
        tool_profile,
        headers: BTreeMap::new(),
    })
}

fn required_profile_field<'a>(value: Option<&'a str>, field: &str) -> Result<&'a str, String> {
    value
        .filter(|value| !value.trim().is_empty())
        .ok_or_else(|| format!("model profile has no {field}"))
}

pub(crate) fn codex_headers(session: &str) -> Result<BTreeMap<String, String>, String> {
    let Some(home) = env::var_os("HOME") else {
        return Err("HOME is not set".to_owned());
    };
    let path = Path::new(&home).join(".codex/auth.json");
    let source = fs::read_to_string(&path).map_err(|error| {
        format!(
            "could not read {}: {error}; run `codex login` once",
            path.display()
        )
    })?;
    let document: Value = serde_json::from_str(&source)
        .map_err(|error| format!("could not parse {}: {error}", path.display()))?;
    let tokens = document
        .get("tokens")
        .and_then(Value::as_object)
        .ok_or_else(|| {
            format!(
                "{} carries no tokens; run `codex login` again",
                path.display()
            )
        })?;
    let access_token = tokens
        .get("access_token")
        .and_then(Value::as_str)
        .filter(|token| !token.is_empty())
        .ok_or_else(|| {
            format!(
                "{} carries no access token; run `codex login` again",
                path.display()
            )
        })?;
    let account_id = tokens
        .get("account_id")
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
        .map(str::to_owned)
        .or_else(|| account_id_from_token(access_token))
        .ok_or_else(|| format!("{} carries no ChatGPT account id", path.display()))?;
    let credentials = zeta_agent::CodexCredentials::new(access_token.to_owned(), account_id);
    Ok(zeta_agent::codex_request_headers(&credentials, session))
}

fn account_id_from_token(token: &str) -> Option<String> {
    let payload = token.split('.').nth(1)?;
    let decoded = base64::engine::general_purpose::URL_SAFE_NO_PAD
        .decode(payload)
        .ok()
        .or_else(|| {
            base64::engine::general_purpose::URL_SAFE
                .decode(format!(
                    "{payload}{}",
                    "=".repeat((4 - payload.len() % 4) % 4)
                ))
                .ok()
        })?;
    let claims: Value = serde_json::from_slice(&decoded).ok()?;
    claims
        .get("https://api.openai.com/auth")
        .and_then(Value::as_object)
        .and_then(|auth| auth.get("chatgpt_account_id"))
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
        .map(str::to_owned)
}

fn parse_profiles(source: &str) -> Result<Vec<ModelProfile>, String> {
    let mut profiles = Vec::new();
    let mut current = None;
    for (index, source_line) in source.lines().enumerate() {
        let line = strip_comment(source_line).trim();
        if line.is_empty() {
            continue;
        }
        if line == "[[models]]" {
            if let Some(profile) = current.take() {
                profiles.push(profile);
            }
            current = Some(ModelProfile::default());
            continue;
        }
        let Some(profile) = current.as_mut() else {
            continue;
        };
        let Some((key, raw_value)) = line.split_once('=') else {
            return Err(format!("models[{}] has an invalid field", index + 1));
        };
        let key = key.trim();
        let value = parse_value(raw_value.trim())?;
        match key {
            "name" => profile.name = Some(value),
            "model" => profile.model = Some(value),
            "url" => profile.url = Some(value),
            "thinking" => profile.thinking = Some(value),
            "api" => profile.api = Some(value),
            "tool_profile" => profile.tool_profile = Some(value),
            "default" => {
                profile.default = match value.as_str() {
                    "true" => true,
                    "false" => false,
                    _ => {
                        return Err(format!(
                            "models[{}].default must be true or false",
                            index + 1
                        ));
                    }
                }
            }
            _ => {}
        }
    }
    if let Some(profile) = current {
        profiles.push(profile);
    }
    let defaults = profiles.iter().filter(|profile| profile.default).count();
    if defaults > 1 {
        return Err("only one model profile may set default = true".to_owned());
    }
    Ok(profiles)
}

fn strip_comment(line: &str) -> &str {
    let mut quote = None;
    for (index, character) in line.char_indices() {
        match (quote, character) {
            (None, '\'' | '"') => quote = Some(character),
            (Some(current), character) if current == character => quote = None,
            (None, '#') => return &line[..index],
            _ => {}
        }
    }
    line
}

fn parse_value(value: &str) -> Result<String, String> {
    if value.len() >= 2 && value.starts_with('"') && value.ends_with('"') {
        return serde_json::from_str(value)
            .map_err(|error| format!("invalid quoted value: {error}"));
    }
    if value.len() >= 2 && value.starts_with('\'') && value.ends_with('\'') {
        return Ok(value[1..value.len() - 1].to_owned());
    }
    if value.is_empty() {
        return Err("a model profile value is empty".to_owned());
    }
    Ok(value.to_owned())
}

#[cfg(test)]
mod tests {
    use std::path::Path;

    use super::{ProfileSource, parse_profiles, select_profile, selection_from_profile};

    #[test]
    fn parses_the_python_model_profile_shape() {
        let profiles = parse_profiles(
            "[[models]]\nname = \"local\"\nmodel = \"qwen\"\nurl = \"http://127.0.0.1/v1/chat/completions\"\ndefault = true\n",
        )
        .expect("profiles");
        let selection =
            selection_from_profile(profiles.first().expect("profile")).expect("selection");
        assert_eq!(selection.name, "qwen");
        assert_eq!(selection.api, "chat-completions");
    }

    #[test]
    fn selects_the_project_profile_before_the_local_profile() {
        let project = parse_profiles(
            "[[models]]\nname = \"fast\"\nmodel = \"project-model\"\nurl = \"http://project/v1/chat/completions\"\n",
        )
        .expect("project profiles");
        let local = parse_profiles(
            "[[models]]\nname = \"fast\"\nmodel = \"local-model\"\nurl = \"http://local/v1/chat/completions\"\n",
        )
        .expect("local profiles");
        let resolved = select_profile(
            Some("fast"),
            project,
            local,
            Path::new("/project/zeta.toml"),
            Path::new("/home/.zeta/models.toml"),
        )
        .expect("profile");
        assert_eq!(resolved.source, ProfileSource::Project);
        assert_eq!(resolved.profile.model.as_deref(), Some("project-model"));
    }

    #[test]
    fn selects_the_local_profile_after_a_project_miss() {
        let local = parse_profiles(
            "[[models]]\nname = \"fast-local\"\nmodel = \"qwen\"\nurl = \"http://127.0.0.1/v1/chat/completions\"\n",
        )
        .expect("profiles");
        let resolved = select_profile(
            Some("fast-local"),
            Vec::new(),
            local,
            Path::new("/project/zeta.toml"),
            Path::new("/home/.zeta/models.toml"),
        )
        .expect("profile");
        assert_eq!(resolved.source, ProfileSource::Local);
    }
}
