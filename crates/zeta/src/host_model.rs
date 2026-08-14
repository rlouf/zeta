//! Resolves the host-owned model selection for native agent runs.

use std::collections::BTreeMap;
use std::env;
use std::fs;
use std::path::Path;

use base64::Engine as _;
use serde_json::Value;
use zeta_agent::ToolProfile;
use zeta_manifest::ModelSpec;

const CODEX_API: &str = "codex-responses";
const CHAT_API: &str = "chat-completions";
const DEFAULT_CODEX_URL: &str = "https://chatgpt.com/backend-api/codex/responses";
const DEFAULT_CODEX_MODEL: &str = "gpt-5.6-sol";

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

/// Resolves an agent model profile or the host default profile.
pub(crate) fn resolve(agent: Option<&ModelSpec>, session: &str) -> Result<ModelSelection, String> {
    let mut selected = match agent {
        Some(ModelSpec::Endpoint { name, url }) => ModelSelection {
            name: name.clone(),
            url: url.clone(),
            api: CHAT_API.to_owned(),
            thinking: None,
            tool_profile: ToolProfile::Native,
            headers: BTreeMap::new(),
        },
        Some(ModelSpec::Profile(name)) => {
            let profiles = configured_profiles()?;
            selection_for_profile(&profiles, name)?
        }
        None => {
            let profiles = configured_profiles()?;
            let profile = profiles.iter().find(|profile| profile.default);
            selection_from_profile(profile)?
        }
    };
    if selected.api == CODEX_API {
        selected.headers = codex_headers(session)?;
    }
    Ok(selected)
}

fn configured_profiles() -> Result<Vec<ModelProfile>, String> {
    let path = model_config_path()?;
    let profiles = match fs::read_to_string(&path) {
        Ok(source) => parse_profiles(&source)
            .map_err(|error| format!("could not read {}: {error}", path.display()))?,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Vec::new(),
        Err(error) => return Err(format!("could not read {}: {error}", path.display())),
    };
    Ok(profiles)
}

fn model_config_path() -> Result<std::path::PathBuf, String> {
    let Some(home) = env::var_os("HOME") else {
        return Err("HOME is not set".to_owned());
    };
    Ok(Path::new(&home).join(".zeta/models.toml"))
}

fn selection_for_profile(profiles: &[ModelProfile], name: &str) -> Result<ModelSelection, String> {
    let profile = profiles
        .iter()
        .find(|profile| profile.name.as_deref() == Some(name))
        .ok_or_else(|| format!("model profile {name:?} is not configured"))?;
    selection_from_profile(Some(profile))
}

fn selection_from_profile(profile: Option<&ModelProfile>) -> Result<ModelSelection, String> {
    let Some(profile) = profile else {
        return Ok(ModelSelection {
            name: DEFAULT_CODEX_MODEL.to_owned(),
            url: DEFAULT_CODEX_URL.to_owned(),
            api: CODEX_API.to_owned(),
            thinking: None,
            tool_profile: ToolProfile::Codex,
            headers: BTreeMap::new(),
        });
    };
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
            ))
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
                        ))
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
    use super::{parse_profiles, selection_for_profile, selection_from_profile, CODEX_API};

    #[test]
    fn parses_the_python_model_profile_shape() {
        let profiles = parse_profiles(
            "[[models]]\nname = \"local\"\nmodel = \"qwen\"\nurl = \"http://127.0.0.1/v1/chat/completions\"\ndefault = true\n",
        )
        .expect("profiles");
        let selection = selection_from_profile(profiles.first()).expect("selection");
        assert_eq!(selection.name, "qwen");
        assert_eq!(selection.api, "chat-completions");
    }

    #[test]
    fn uses_the_codex_defaults_without_a_profile() {
        let selection = selection_from_profile(None).expect("selection");
        assert_eq!(selection.api, CODEX_API);
        assert_eq!(selection.name, "gpt-5.6-sol");
    }

    #[test]
    fn resolves_a_named_profile() {
        let profiles = parse_profiles(
            "[[models]]\nname = \"fast-local\"\nmodel = \"qwen\"\nurl = \"http://127.0.0.1/v1/chat/completions\"\n",
        )
        .expect("profiles");
        let selection = selection_for_profile(&profiles, "fast-local").expect("selection");
        assert_eq!(selection.name, "qwen");
    }
}
