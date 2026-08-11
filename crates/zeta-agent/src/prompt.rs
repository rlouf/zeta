//! Pure prompt projection, rendering, and trace construction.

use std::collections::{HashMap, HashSet};

use serde::{Deserialize, Serialize};
use serde_json::{json, Map, Value};
use zeta_substrate::{canonical_json, hash_bytes, Derivation, Object};

use crate::capability::CapabilityId;
use crate::error::AgentError;
use crate::invocation::PromptEnvironment;
use crate::model::ModelInput;
use crate::trace::{AddressedDerivation, AddressedObject, TraceBatch};

const TOOL_PROTOCOL: &str = "Tool protocol:\n\n- Tools are native Chat Completions function tools exposed by the runtime.\n- You may request multiple read-only tool calls in one turn when useful.\n- Mutating tools apply their effects when you call them.\n- Use a tool only when its schema matches the needed action.\n- Do not mention unavailable tools.\n- If no tool is needed, return a final answer.";
const GREP_POLICY: &str =
    "Use `grep` to locate occurrences before reading files when the target text/symbol is known.";

fn default_tool_choice() -> Value {
    Value::String("auto".to_owned())
}

fn default_max_tokens() -> u64 {
    8_192
}

/// Supplies every pure input to one prompt projection.
#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
pub struct PromptInput {
    /// States the current objective.
    pub objective: String,
    /// Carries the prior normalized timeline.
    #[serde(default)]
    pub timeline: Vec<Map<String, Value>>,
    /// Supplies the caller-owned base system prompt.
    #[serde(default)]
    pub system: Option<String>,
    /// Lists canonical capabilities in caller order.
    #[serde(default)]
    pub allowed_capabilities: Vec<CapabilityId>,
    /// Adds explicit project or caller context.
    #[serde(default)]
    pub context: String,
    /// Lists model-facing tool descriptors.
    #[serde(default)]
    pub tools: Vec<Value>,
    /// Selects the model's tool behavior.
    #[serde(default = "default_tool_choice")]
    pub tool_choice: Value,
    /// Bounds completion output tokens.
    #[serde(default = "default_max_tokens")]
    pub max_tokens: u64,
    /// Selects the resolved model.
    #[serde(default)]
    pub selected_model: Option<String>,
    /// Carries the resolved reasoning option.
    #[serde(default)]
    pub thinking: Option<String>,
    /// Carries proposals produced earlier in the same invocation.
    #[serde(default)]
    pub current_events: Vec<Map<String, Value>>,
}

impl Default for PromptInput {
    fn default() -> Self {
        PromptInput {
            objective: String::new(),
            timeline: Vec::new(),
            system: None,
            allowed_capabilities: Vec::new(),
            context: String::new(),
            tools: Vec::new(),
            tool_choice: default_tool_choice(),
            max_tokens: default_max_tokens(),
            selected_model: None,
            thinking: None,
            current_events: Vec::new(),
        }
    }
}

/// Carries one prompt component before or after trace addressing.
#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
pub struct PromptComponent {
    /// Names the component's role in prompt construction.
    pub kind: String,
    /// Carries the complete trace payload.
    pub data: Map<String, Value>,
    /// Carries an optional model-facing chat message.
    pub message: Option<Map<String, Value>>,
    /// Names the full, summary, or stub representation.
    pub representation: String,
    /// Identifies the source object when a transform created this component.
    pub source_object_id: Option<String>,
    /// Lists structural source objects.
    pub links: Vec<String>,
    /// Identifies the addressed component after trace construction.
    pub object_id: Option<String>,
}

/// Returns one fully addressed prompt and its exact model request.
#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
pub struct PromptBuild {
    /// Preserves component order and assigned object ids.
    pub components: Vec<PromptComponent>,
    /// Carries the provider-neutral model input.
    pub model_input: ModelInput,
    /// Carries the exact OpenAI-compatible request projection.
    pub request_payload: Map<String, Value>,
    /// Identifies the complete prompt object.
    pub prompt_object_id: String,
    /// Preserves component identities in prompt order.
    pub component_object_ids: Vec<String>,
    /// Lists trace objects in address order.
    pub objects: Vec<AddressedObject>,
    /// Lists trace derivations in address order.
    pub derivations: Vec<AddressedDerivation>,
}

#[derive(Clone)]
struct MessageEntry {
    event_index: usize,
    event: Map<String, Value>,
    message: Map<String, Value>,
}

/// Builds and addresses one deterministic prompt.
///
/// # Examples
///
/// ```
/// use zeta_agent::{build_prompt, PromptEnvironment, PromptInput};
///
/// let input = PromptInput {
///     objective: "Say hello.".to_owned(),
///     ..PromptInput::default()
/// };
/// let environment = PromptEnvironment {
///     working_directory: "/workspace/zeta".to_owned(),
///     calendar_date: "2026-08-12".to_owned(),
/// };
/// let prompt = build_prompt(&input, &environment).unwrap();
/// assert_eq!(prompt.model_input.messages.len(), 2);
/// ```
///
/// # Errors
///
/// Returns [`AgentError`] when the environment date is invalid or a trace
/// value cannot use the shared canonical encoding.
pub fn build_prompt(
    input: &PromptInput,
    environment: &PromptEnvironment,
) -> Result<PromptBuild, AgentError> {
    let PromptInput {
        objective,
        timeline,
        system,
        allowed_capabilities,
        context,
        tools,
        tool_choice,
        max_tokens,
        selected_model,
        thinking,
        current_events,
    } = input;
    let system_content = render_system_prompt(
        system.as_deref(),
        allowed_capabilities,
        tools,
        &environment.calendar_date,
    )?;
    let mut allowed_tools = Vec::new();
    for id in allowed_capabilities {
        allowed_tools.push(Value::String(id.to_string()));
    }
    let mut components = vec![PromptComponent {
        kind: "system_prompt".to_owned(),
        data: object(json!({
            "content": system_content,
            "base_prompt": system,
            "allowed_tools": allowed_tools,
        })),
        message: Some(object(json!({
            "role": "system",
            "content": system_content,
        }))),
        representation: "full".to_owned(),
        source_object_id: None,
        links: Vec::new(),
        object_id: None,
    }];
    components.push(PromptComponent {
        kind: "tool_descriptor_set".to_owned(),
        data: object(json!({
            "allowed_tools": allowed_tools,
            "tools": tools,
        })),
        message: None,
        representation: "full".to_owned(),
        source_object_id: None,
        links: Vec::new(),
        object_id: None,
    });
    let context = context.trim();
    if !context.is_empty() {
        components.push(PromptComponent {
            kind: "project_context".to_owned(),
            data: object(json!({
                "content_address": hash_bytes(context.as_bytes()).to_string(),
                "chars": context.chars().count(),
            })),
            message: None,
            representation: "full".to_owned(),
            source_object_id: None,
            links: Vec::new(),
            object_id: None,
        });
    }
    let timeline = from_message_boundary(timeline);
    components.extend(project_timeline(&timeline, true)?);
    let mut objective_sections = vec![
        objective.clone(),
        format!("cwd:\n{}", environment.working_directory),
    ];
    if !context.is_empty() {
        objective_sections.push(context.to_owned());
    }
    let objective_message = objective_sections.join("\n\n");
    components.push(PromptComponent {
        kind: "user_message".to_owned(),
        data: object(json!({
            "objective": objective,
            "expanded_objective": objective,
            "context": context,
            "message": {
                "role": "user",
                "content": objective_message,
            },
        })),
        message: Some(object(json!({
            "role": "user",
            "content": objective_message,
        }))),
        representation: "full".to_owned(),
        source_object_id: None,
        links: Vec::new(),
        object_id: None,
    });
    components.extend(project_timeline(current_events, false)?);

    let mut trace = TraceBatch::default();
    let mut component_object_ids = Vec::new();
    for component in &mut components {
        let mut data = component.data.clone();
        if !data.contains_key("message") {
            if let Some(message) = &component.message {
                data.insert("message".to_owned(), Value::Object(message.clone()));
            }
        }
        data.insert(
            "representation".to_owned(),
            Value::String(component.representation.clone()),
        );
        if let Some(source_object_id) = &component.source_object_id {
            data.insert(
                "source_object_id".to_owned(),
                Value::String(source_object_id.clone()),
            );
        }
        let object_id = trace.insert_object(Object {
            kind: component.kind.clone(),
            schema: "zeta.prompt_component.v2".to_owned(),
            data,
            links: component.links.clone(),
        })?;
        component.object_id = Some(object_id.clone());
        component_object_ids.push(object_id);
    }
    let messages = component_messages(&components);
    let model_input = ModelInput {
        messages: messages.clone(),
        tools: tools.clone(),
        tool_choice: tool_choice.clone(),
        max_tokens: *max_tokens,
        selected_model: selected_model.clone(),
        selected_url: None,
        session_id: None,
        thinking: thinking.clone(),
    };
    let request_payload = request_payload(&model_input);
    let payload_bytes = canonical_json(&Value::Object(request_payload.clone()))
        .map_err(|error| AgentError::trace(error.to_string()))?;
    let prompt_object_id = trace.insert_object(Object {
        kind: "prompt".to_owned(),
        schema: "zeta.prompt.v2".to_owned(),
        data: object(json!({
            "payload_address": hash_bytes(&payload_bytes).to_string(),
        })),
        links: component_object_ids.clone(),
    })?;
    trace.insert_derivation(Derivation {
        producer: "PromptBuilder".to_owned(),
        output_id: prompt_object_id.clone(),
        input_ids: component_object_ids.clone(),
        params: object(json!({
            "max_tokens": max_tokens,
            "selected_model": selected_model,
            "thinking": thinking,
        })),
    })?;
    Ok(PromptBuild {
        components,
        model_input,
        request_payload,
        prompt_object_id,
        component_object_ids,
        objects: trace.objects,
        derivations: trace.derivations,
    })
}

fn render_system_prompt(
    base_prompt: Option<&str>,
    allowed_capabilities: &[CapabilityId],
    tools: &[Value],
    calendar_date: &str,
) -> Result<String, AgentError> {
    let mut sections = Vec::new();
    let base_prompt = base_prompt.unwrap_or("").trim();
    if !base_prompt.is_empty() {
        sections.push(base_prompt.to_owned());
    }
    sections.push(calendar_date_line(calendar_date)?);
    sections.push(TOOL_PROTOCOL.to_owned());
    if tool_available("grep", tools) {
        sections.push(format!("Tool policy:\n\n- {GREP_POLICY}"));
    }
    sections.push(tools_prompt(allowed_capabilities, tools));
    Ok(sections.join("\n\n"))
}

fn calendar_date_line(value: &str) -> Result<String, AgentError> {
    let mut parts = value.split('-');
    let Some(year) = parts.next() else {
        return Err(AgentError::prompt("calendar date must use YYYY-MM-DD"));
    };
    let Some(month) = parts.next() else {
        return Err(AgentError::prompt("calendar date must use YYYY-MM-DD"));
    };
    let Some(day) = parts.next() else {
        return Err(AgentError::prompt("calendar date must use YYYY-MM-DD"));
    };
    if parts.next().is_some() || year.len() != 4 || month.len() != 2 || day.len() != 2 {
        return Err(AgentError::prompt("calendar date must use YYYY-MM-DD"));
    }
    let year = year
        .parse::<i32>()
        .map_err(|_error| AgentError::prompt("calendar date year is invalid"))?;
    let month = month
        .parse::<u32>()
        .map_err(|_error| AgentError::prompt("calendar date month is invalid"))?;
    let day = day
        .parse::<u32>()
        .map_err(|_error| AgentError::prompt("calendar date day is invalid"))?;
    validate_date(year, month, day)?;
    let weekday = weekday(year, month, day);
    Ok(format!("Today is {value} ({weekday})."))
}

fn validate_date(year: i32, month: u32, day: u32) -> Result<(), AgentError> {
    if year == 0 || !(1..=12).contains(&month) {
        return Err(AgentError::prompt("calendar date is invalid"));
    }
    let leap = year % 4 == 0 && (year % 100 != 0 || year % 400 == 0);
    let days = match month {
        1 => 31,
        2 if leap => 29,
        2 => 28,
        3 => 31,
        4 => 30,
        5 => 31,
        6 => 30,
        7 => 31,
        8 => 31,
        9 => 30,
        10 => 31,
        11 => 30,
        12 => 31,
        0 | 13..=u32::MAX => return Err(AgentError::prompt("calendar date is invalid")),
    };
    if day == 0 || day > days {
        return Err(AgentError::prompt("calendar date is invalid"));
    }
    Ok(())
}

fn weekday(year: i32, month: u32, day: u32) -> &'static str {
    let year = year - i32::from(month <= 2);
    let era = year.div_euclid(400);
    let year_of_era = year - era * 400;
    let month = month as i32;
    let day = day as i32;
    let shifted_month = month + if month > 2 { -3 } else { 9 };
    let day_of_year = (153 * shifted_month + 2) / 5 + day - 1;
    let day_of_era = year_of_era * 365 + year_of_era / 4 - year_of_era / 100 + day_of_year;
    let unix_days = era * 146_097 + day_of_era - 719_468;
    let weekday = (unix_days + 4).rem_euclid(7) as usize;
    [
        "Sunday",
        "Monday",
        "Tuesday",
        "Wednesday",
        "Thursday",
        "Friday",
        "Saturday",
    ][weekday]
}

fn tool_available(name: &str, tools: &[Value]) -> bool {
    for tool in tools {
        let Some(function) = tool.get("function").and_then(Value::as_object) else {
            continue;
        };
        if function.get("name").and_then(Value::as_str) == Some(name) {
            return true;
        }
    }
    false
}

fn tools_prompt(_allowed_capabilities: &[CapabilityId], tools: &[Value]) -> String {
    if tools.is_empty() {
        return "Available tools:\n(none)".to_owned();
    }
    let mut lines = vec!["Available tools:".to_owned()];
    for tool in tools {
        let Some(function) = tool.get("function").and_then(Value::as_object) else {
            lines.push("- unknown()".to_owned());
            continue;
        };
        let name = function
            .get("name")
            .and_then(Value::as_str)
            .unwrap_or("unknown");
        let description = function
            .get("description")
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim();
        let schema = function
            .get("parameters")
            .and_then(Value::as_object)
            .cloned()
            .unwrap_or_default();
        let signature = tool_signature(name, &schema);
        if description.is_empty() {
            lines.push(format!("- {signature}"));
        } else {
            lines.push(format!("- {signature}: {description}"));
        }
    }
    lines.join("\n")
}

fn tool_signature(name: &str, schema: &Map<String, Value>) -> String {
    let Some(properties) = schema.get("properties").and_then(Value::as_object) else {
        return format!("{name}()");
    };
    if properties.is_empty() {
        return format!("{name}()");
    }
    let mut required = HashSet::new();
    if let Some(values) = schema.get("required").and_then(Value::as_array) {
        for value in values {
            if let Some(value) = value.as_str() {
                required.insert(value);
            }
        }
    }
    let mut arguments = Vec::new();
    for property in properties.keys() {
        if required.contains(property.as_str()) {
            arguments.push(property.clone());
        }
    }
    for property in properties.keys() {
        if !required.contains(property.as_str()) {
            arguments.push(format!("{property}?"));
        }
    }
    format!("{name}({})", arguments.join(", "))
}

fn from_message_boundary(events: &[Map<String, Value>]) -> Vec<Map<String, Value>> {
    let mut start = 0;
    while start < events.len() && event_text(&events[start], "type") == "tool_result" {
        start += 1;
    }
    events[start..].to_vec()
}

fn project_timeline(
    events: &[Map<String, Value>],
    historical: bool,
) -> Result<Vec<PromptComponent>, AgentError> {
    let entries = message_entries(events)?;
    let entries = answered_entries(entries);
    let mut components = Vec::new();
    let mut tool_names: HashMap<String, String> = HashMap::new();
    for (message_index, entry) in entries.into_iter().enumerate() {
        let role = event_text(&entry.message, "role");
        let kind = if role == "tool" {
            "tool_result"
        } else if role == "assistant" {
            "assistant_message"
        } else {
            "user_message"
        };
        let tool_call_id = event_text(&entry.event, "tool_call_id");
        let tool_name = tool_names.get(tool_call_id).cloned().unwrap_or_default();
        let data = timeline_component_data(message_index, &entry, &tool_name, historical);
        let links = timeline_component_links(&entry.event);
        record_tool_names(&entry.message, &mut tool_names);
        components.push(PromptComponent {
            kind: kind.to_owned(),
            data,
            message: Some(entry.message),
            representation: "full".to_owned(),
            source_object_id: None,
            links,
            object_id: None,
        });
    }
    Ok(components)
}

fn message_entries(events: &[Map<String, Value>]) -> Result<Vec<MessageEntry>, AgentError> {
    let mut entries = Vec::new();
    let mut tool_call_ids = HashSet::new();
    for (index, event) in events.iter().enumerate() {
        let message = project_message(event, index, &tool_call_ids)?;
        let Some(message) = message else { continue };
        record_call_ids(&message, &mut tool_call_ids);
        entries.push(MessageEntry {
            event_index: index,
            event: event.clone(),
            message,
        });
    }
    Ok(entries)
}

fn project_message(
    event: &Map<String, Value>,
    index: usize,
    tool_call_ids: &HashSet<String>,
) -> Result<Option<Map<String, Value>>, AgentError> {
    let mut role = event_text(event, "role");
    let event_type = event_text(event, "type");
    if role != "user" && role != "assistant" {
        if event_type == "user_message" {
            role = "user";
        } else if event_type == "model" || event_type == "turn_aborted" {
            role = "assistant";
        } else {
            role = "";
        }
    }
    if !role.is_empty() {
        let content = event_text(event, "content");
        if role == "assistant" {
            if let Some(tool_calls) = event.get("tool_calls").and_then(Value::as_array) {
                let content = if content.is_empty() {
                    Value::Null
                } else {
                    Value::String(content.to_owned())
                };
                return Ok(Some(object(json!({
                    "role": "assistant",
                    "content": content,
                    "tool_calls": repair_tool_calls(tool_calls),
                }))));
            }
        }
        if content.is_empty() {
            return Ok(None);
        }
        return Ok(Some(object(json!({
            "role": role,
            "content": content,
        }))));
    }
    if event_type == "tool_call" {
        let call_id = nonempty_event_text(event, "id")
            .or_else(|| nonempty_event_text(event, "tool_call_id"))
            .unwrap_or_else(|| format!("call-{index}"));
        if tool_call_ids.contains(&call_id) {
            return Ok(None);
        }
        let input = event
            .get("input")
            .and_then(Value::as_object)
            .cloned()
            .unwrap_or_default();
        return Ok(Some(object(json!({
            "role": "assistant",
            "content": null,
            "tool_calls": [{
                "id": call_id,
                "type": "function",
                "function": {
                    "name": event_text(event, "name"),
                    "arguments": compact_json(&Value::Object(input))?,
                },
            }],
        }))));
    }
    if event_type == "tool_result" {
        let call_id = event_text(event, "tool_call_id");
        if !call_id.is_empty() && tool_call_ids.contains(call_id) {
            let result = event
                .get("result")
                .and_then(Value::as_object)
                .cloned()
                .unwrap_or_default();
            return Ok(Some(object(json!({
                "role": "tool",
                "tool_call_id": call_id,
                "content": runtime_json_object(&result)?,
            }))));
        }
        return Ok(Some(object(json!({
            "role": "user",
            "content": format!("Tool result JSON:\n{}", compact_json(&Value::Object(event.clone()))?),
        }))));
    }
    Ok(None)
}

fn repair_tool_calls(tool_calls: &[Value]) -> Vec<Value> {
    let mut repaired = Vec::new();
    for call in tool_calls {
        let Some(call_map) = call.as_object() else {
            repaired.push(call.clone());
            continue;
        };
        let Some(function) = call_map.get("function").and_then(Value::as_object) else {
            repaired.push(call.clone());
            continue;
        };
        let Some(arguments) = function.get("arguments").and_then(Value::as_str) else {
            repaired.push(call.clone());
            continue;
        };
        if serde_json::from_str::<Value>(arguments).is_ok() {
            repaired.push(call.clone());
            continue;
        }
        let mut function = function.clone();
        let mut truncated_arguments = String::new();
        for (character_count, character) in arguments.chars().enumerate() {
            if character_count == 200 {
                break;
            }
            truncated_arguments.push(character);
        }
        function.insert(
            "arguments".to_owned(),
            Value::String(format!(
                "{{\"truncated_arguments\": {}}}",
                serde_json::to_string(&truncated_arguments)
                    .expect("a JSON string payload always serializes"),
            )),
        );
        let mut call = call_map.clone();
        call.insert("function".to_owned(), Value::Object(function));
        repaired.push(Value::Object(call));
    }
    repaired
}

fn answered_entries(entries: Vec<MessageEntry>) -> Vec<MessageEntry> {
    let mut answered_ids = HashSet::new();
    for entry in &entries {
        if event_text(&entry.message, "role") == "tool" {
            let id = event_text(&entry.message, "tool_call_id");
            answered_ids.insert(id.to_owned());
        }
    }
    let mut kept = Vec::new();
    let mut kept_call_ids = HashSet::new();
    for entry in entries {
        if has_unanswered_call(&entry.message, &answered_ids) {
            continue;
        }
        record_call_ids(&entry.message, &mut kept_call_ids);
        kept.push(entry);
    }
    let mut answered = Vec::new();
    for entry in kept {
        if event_text(&entry.message, "role") != "tool"
            || kept_call_ids.contains(event_text(&entry.message, "tool_call_id"))
        {
            answered.push(entry);
        }
    }
    answered
}

fn has_unanswered_call(message: &Map<String, Value>, answered: &HashSet<String>) -> bool {
    if event_text(message, "role") != "assistant" {
        return false;
    }
    let Some(calls) = message.get("tool_calls").and_then(Value::as_array) else {
        return false;
    };
    for call in calls {
        let id = call.get("id").and_then(Value::as_str).unwrap_or("");
        if !id.is_empty() && !answered.contains(id) {
            return true;
        }
    }
    false
}

fn record_call_ids(message: &Map<String, Value>, ids: &mut HashSet<String>) {
    let Some(calls) = message.get("tool_calls").and_then(Value::as_array) else {
        return;
    };
    for call in calls {
        let Some(id) = call.get("id").and_then(Value::as_str) else {
            continue;
        };
        if !id.is_empty() {
            ids.insert(id.to_owned());
        }
    }
}

fn record_tool_names(message: &Map<String, Value>, names: &mut HashMap<String, String>) {
    let Some(calls) = message.get("tool_calls").and_then(Value::as_array) else {
        return;
    };
    for call in calls {
        let Some(id) = call.get("id").and_then(Value::as_str) else {
            continue;
        };
        let Some(name) = call
            .get("function")
            .and_then(Value::as_object)
            .and_then(|function| function.get("name"))
            .and_then(Value::as_str)
        else {
            continue;
        };
        if !id.is_empty() && !name.is_empty() {
            names.insert(id.to_owned(), name.to_owned());
        }
    }
}

fn timeline_component_data(
    message_index: usize,
    entry: &MessageEntry,
    tool_name: &str,
    historical: bool,
) -> Map<String, Value> {
    let mut data = object(json!({
        "index": message_index,
        "event_index": entry.event_index,
        "message": entry.message,
        "source_event_type": event_text(&entry.event, "type"),
        "source_event_role": event_text(&entry.event, "role"),
    }));
    if historical {
        data.insert("historical".to_owned(), Value::Bool(true));
    }
    if !tool_name.is_empty() {
        data.insert(
            "source_tool_name".to_owned(),
            Value::String(tool_name.to_owned()),
        );
    }
    let event_type = event_text(&entry.event, "type");
    if event_type == "tool_result" {
        data.insert(
            "source_tool_call_id".to_owned(),
            Value::String(event_text(&entry.event, "tool_call_id").to_owned()),
        );
        if let Some(result) = entry.event.get("result").and_then(Value::as_object) {
            data.insert(
                "source_tool_result".to_owned(),
                Value::Object(result.clone()),
            );
        }
        copy_source_id(
            &entry.event,
            &mut data,
            "tool_result_object_id",
            "source_tool_result_object_id",
        );
        copy_source_id(
            &entry.event,
            &mut data,
            "tool_call_object_id",
            "source_tool_call_object_id",
        );
    } else if event_type == "tool_call" {
        let call_id = nonempty_event_text(&entry.event, "tool_call_id")
            .or_else(|| nonempty_event_text(&entry.event, "id"))
            .unwrap_or_default();
        data.insert("source_tool_call_id".to_owned(), Value::String(call_id));
        data.insert(
            "source_tool_name".to_owned(),
            Value::String(event_text(&entry.event, "name").to_owned()),
        );
        let input = entry
            .event
            .get("input")
            .and_then(Value::as_object)
            .cloned()
            .unwrap_or_default();
        data.insert("source_tool_input".to_owned(), Value::Object(input));
        copy_source_id(
            &entry.event,
            &mut data,
            "tool_call_object_id",
            "source_tool_call_object_id",
        );
    } else if event_type == "model" {
        if let Some(tool_calls) = entry.event.get("tool_calls").and_then(Value::as_array) {
            data.insert(
                "source_model_tool_calls".to_owned(),
                Value::Array(tool_calls.clone()),
            );
        }
        if let Some(assistant_id) = entry
            .event
            .get("prompt_trace")
            .and_then(Value::as_object)
            .and_then(|trace| trace.get("assistant_message_object_id"))
            .and_then(Value::as_str)
        {
            data.insert(
                "source_assistant_message_object_id".to_owned(),
                Value::String(assistant_id.to_owned()),
            );
        }
    }
    data
}

fn copy_source_id(
    event: &Map<String, Value>,
    data: &mut Map<String, Value>,
    source: &str,
    destination: &str,
) {
    if let Some(value) = event.get(source) {
        data.insert(destination.to_owned(), value.clone());
    }
}

fn timeline_component_links(event: &Map<String, Value>) -> Vec<String> {
    let mut links = Vec::new();
    if let Some(assistant_id) = event
        .get("prompt_trace")
        .and_then(Value::as_object)
        .and_then(|trace| trace.get("assistant_message_object_id"))
        .and_then(Value::as_str)
    {
        if !assistant_id.is_empty() {
            links.push(assistant_id.to_owned());
        }
    }
    for field in ["tool_result_object_id", "tool_call_object_id"] {
        let Some(id) = event.get(field).and_then(Value::as_str) else {
            continue;
        };
        if !id.is_empty() && !contains_string(&links, id) {
            links.push(id.to_owned());
        }
    }
    links
}

fn component_messages(components: &[PromptComponent]) -> Vec<Map<String, Value>> {
    let mut messages = Vec::new();
    for component in components {
        if let Some(message) = &component.message {
            messages.push(message.clone());
        }
    }
    messages
}

fn request_payload(input: &ModelInput) -> Map<String, Value> {
    let ModelInput {
        messages,
        tools,
        tool_choice,
        max_tokens,
        selected_model,
        selected_url: _selected_url,
        session_id: _session_id,
        thinking,
    } = input;
    let mut payload = object(json!({
        "model": selected_model.clone().unwrap_or_default(),
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": max_tokens,
        "stream_options": {"include_usage": true},
    }));
    match thinking.as_deref() {
        Some("none") => {
            payload.insert(
                "chat_template_kwargs".to_owned(),
                json!({"enable_thinking": false}),
            );
        }
        Some(thinking) => {
            payload.insert(
                "reasoning_effort".to_owned(),
                Value::String(thinking.to_owned()),
            );
        }
        None => {}
    }
    if !tools.is_empty() {
        payload.insert("tools".to_owned(), Value::Array(tools.clone()));
        payload.insert("tool_choice".to_owned(), tool_choice.clone());
    }
    payload
}

fn runtime_json_object(value: &Map<String, Value>) -> Result<String, AgentError> {
    let priorities = ["ok", "content", "metadata", "error", "handle", "stop"];
    let mut keys = Vec::new();
    for priority in priorities {
        if value.contains_key(priority) {
            keys.push(priority.to_owned());
        }
    }
    for key in value.keys() {
        if !contains_string(&keys, key) {
            keys.push(key.clone());
        }
    }
    let mut output = String::from("{");
    for (index, key) in keys.iter().enumerate() {
        if index > 0 {
            output.push(',');
        }
        output.push_str(
            &serde_json::to_string(key).map_err(|error| AgentError::prompt(error.to_string()))?,
        );
        output.push(':');
        output.push_str(&compact_json(&value[key])?);
    }
    output.push('}');
    Ok(output)
}

fn compact_json(value: &Value) -> Result<String, AgentError> {
    serde_json::to_string(value).map_err(|error| AgentError::prompt(error.to_string()))
}

fn event_text<'a>(event: &'a Map<String, Value>, key: &str) -> &'a str {
    event.get(key).and_then(Value::as_str).unwrap_or("")
}

fn nonempty_event_text(event: &Map<String, Value>, key: &str) -> Option<String> {
    let value = event_text(event, key);
    if value.is_empty() {
        None
    } else {
        Some(value.to_owned())
    }
}

fn contains_string(values: &[String], expected: &str) -> bool {
    for value in values {
        if value == expected {
            return true;
        }
    }
    false
}

fn object(value: Value) -> Map<String, Value> {
    value
        .as_object()
        .expect("internal prompt JSON value is an object")
        .clone()
}
