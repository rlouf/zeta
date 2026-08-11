//! Model and capability state machine for one resolved invocation.

use std::collections::{HashMap, HashSet};

use serde_json::{json, Map, Value};
use zeta_journal::DraftEvent;
use zeta_substrate::{canonical_json, derive, Derivation, Domain, Object};

use crate::capability::{
    Capability, CapabilityId, CapabilityInvocation, DeliverySemantics, EffectEvent, EffectRecorder,
    EffectStatus, IdSource, ToolExecutor,
};
use crate::control::AgentRequest;
use crate::error::{AgentError, AgentRunAborted, AgentRunError};
use crate::invocation::AgentInvocation;
use crate::model::{AbortReason, AbortSignal, AgentObserver, Clock, ModelGateway, ModelRequest};
use crate::prompt::{build_prompt, PromptBuild, PromptInput};
use crate::result::{AgentRunResult, RunStopReason, StepName};
use crate::trace::{PromptTrace, TraceBatch};

/// Owns borrowed runtime services for one provider-neutral invocation.
pub struct AgentRunner<'a> {
    capabilities: &'a [Capability],
    model_gateway: &'a mut dyn ModelGateway,
    tool_executor: &'a mut dyn ToolExecutor,
    observer: &'a mut dyn AgentObserver,
    effect_recorder: &'a mut dyn EffectRecorder,
    id_source: &'a mut dyn IdSource,
    abort: &'a dyn AbortSignal,
    clock: &'a dyn Clock,
}

impl<'a> AgentRunner<'a> {
    /// Creates a runner around caller-owned runtime services.
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        capabilities: &'a [Capability],
        model_gateway: &'a mut dyn ModelGateway,
        tool_executor: &'a mut dyn ToolExecutor,
        observer: &'a mut dyn AgentObserver,
        effect_recorder: &'a mut dyn EffectRecorder,
        id_source: &'a mut dyn IdSource,
        abort: &'a dyn AbortSignal,
        clock: &'a dyn Clock,
    ) -> Self {
        AgentRunner {
            capabilities,
            model_gateway,
            tool_executor,
            observer,
            effect_recorder,
            id_source,
            abort,
            clock,
        }
    }

    /// Executes one resolved invocation.
    ///
    /// # Errors
    ///
    /// Returns [`AgentRunError`] for a cooperative abort or a boundary failure.
    pub async fn run(
        mut self,
        invocation: &AgentInvocation,
    ) -> Result<AgentRunResult, AgentRunError> {
        let capabilities = CapabilitySet::new(invocation, self.capabilities)?;
        let mut result = AgentRunResult::default();
        let mut state = RunState {
            next_model_caused_by: invocation.caused_by.clone(),
            ..RunState::default()
        };
        loop {
            if !state.pending_tool_calls.is_empty() {
                let tool_calls = std::mem::take(&mut state.pending_tool_calls);
                let model_telemetry = std::mem::take(&mut state.pending_model_telemetry);
                let assistant_event_id = state.pending_tool_parent_id.take();
                for (index, tool_call) in tool_calls.into_iter().enumerate() {
                    result.steps.push(StepName::CheckBudget);
                    if let Some(reason) = self.abort_reason(invocation) {
                        return abort_run(result, reason, state.next_model_caused_by);
                    }
                    result.steps.push(StepName::RecordCapabilityCall);
                    result.steps.push(StepName::ExecuteCapability);
                    let position = state.next_tool_position;
                    state.next_tool_position += 1;
                    let telemetry = if index == 0 {
                        model_telemetry.clone()
                    } else {
                        Map::new()
                    };
                    let outcome = self
                        .execute_tool_call(
                            invocation,
                            &capabilities,
                            tool_call,
                            index,
                            position,
                            assistant_event_id.as_deref(),
                            telemetry,
                            &mut result,
                            &mut state.projection,
                        )
                        .await?;
                    result.steps.push(StepName::RecordCapabilityResult);
                    state.next_model_caused_by = Some(outcome.result_event_id);
                    if outcome.stop {
                        result.stop_reason = Some(RunStopReason::ToolStop);
                        result.steps.push(StepName::FinishRun);
                        return Ok(result);
                    }
                }
                continue;
            }
            if state.model_calls >= invocation.max_model_calls {
                result.stop_reason = Some(RunStopReason::MaxTurns);
                result.steps.push(StepName::FinishRun);
                return Ok(result);
            }
            result.steps.push(StepName::CheckBudget);
            if let Some(reason) = self.abort_reason(invocation) {
                return abort_run(result, reason, state.next_model_caused_by);
            }
            result.steps.push(StepName::BuildPrompt);
            let current_events = current_event_views(&result.events, &state.projection);
            let prompt = build_prompt(
                &PromptInput {
                    objective: invocation.objective.clone(),
                    timeline: invocation.timeline.clone(),
                    system: invocation.system_prompt.clone(),
                    allowed_capabilities: capabilities.allowed_ids.clone(),
                    context: invocation.context.clone(),
                    tools: capabilities.descriptors.clone(),
                    tool_choice: invocation.tool_choice.clone(),
                    max_tokens: invocation.max_tokens,
                    selected_model: invocation.model_name.clone(),
                    thinking: invocation.thinking.clone(),
                    current_events,
                },
                &invocation.environment,
            )?;
            let PromptBuild {
                components: _components,
                model_input,
                request_payload: _request_payload,
                prompt_object_id,
                component_object_ids: _component_object_ids,
                objects,
                derivations,
            } = prompt;
            result.trace.merge(TraceBatch {
                objects,
                derivations,
            })?;
            result.steps.push(StepName::CallModel);
            let active_abort = RunAbort {
                external: self.abort,
                clock: self.clock,
                deadline_ms: invocation.deadline_ms,
            };
            let output = self
                .model_gateway
                .generate(
                    &model_input,
                    &ModelRequest {
                        api: invocation.model_api.clone(),
                        model: invocation.model_name.clone(),
                        url: invocation.model_url.clone(),
                        thinking: invocation.thinking.clone(),
                        session_id: invocation.model_session_id.clone(),
                    },
                    self.observer,
                    &active_abort,
                )
                .await;
            let output = match output {
                Ok(output) => output,
                Err(error) => {
                    if let Some(reason) = active_abort.reason() {
                        return abort_run(result, reason, state.next_model_caused_by.clone());
                    }
                    return Err(error.into());
                }
            };
            result.steps.push(StepName::RecordAssistant);
            if !output.telemetry.is_empty() {
                result.telemetry = output.telemetry.clone();
                result.model_telemetry_calls.push(output.telemetry.clone());
            }
            let event_id = next_event_id(self.id_source)?;
            let model_payload = model_payload(&output.message, &prompt_object_id);
            let draft = model_draft(
                model_payload.clone(),
                &event_id,
                state.next_model_caused_by.clone(),
            );
            result.events.push(draft);
            let assistant_object_id =
                record_model_trace(&mut result.trace, &prompt_object_id, &model_payload)?;
            state.projection.models.insert(
                event_id.clone(),
                (prompt_object_id.clone(), assistant_object_id.clone()),
            );
            state.projection.latest_assistant = Some(assistant_object_id.clone());
            result.prompt_traces.push(PromptTrace {
                prompt_object_id,
                assistant_message_object_id: Some(assistant_object_id),
            });
            state.model_calls += 1;
            let tool_calls = assistant_tool_calls(&output.message);
            if tool_calls.is_empty() {
                result.final_answer = output
                    .message
                    .get("content")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_owned();
                result.answer_streamed = output.streamed_content;
                result.stop_reason = Some(RunStopReason::Finished);
                result.steps.push(StepName::FinishRun);
                return Ok(result);
            }
            state.pending_tool_calls = tool_calls;
            state.pending_model_telemetry = output.telemetry;
            state.pending_tool_parent_id = Some(event_id);
        }
    }

    fn abort_reason(&self, invocation: &AgentInvocation) -> Option<AbortReason> {
        RunAbort {
            external: self.abort,
            clock: self.clock,
            deadline_ms: invocation.deadline_ms,
        }
        .reason()
    }

    #[allow(clippy::too_many_arguments)]
    async fn execute_tool_call(
        &mut self,
        invocation: &AgentInvocation,
        capabilities: &CapabilitySet,
        tool_call: Value,
        index: usize,
        position: usize,
        assistant_event_id: Option<&str>,
        model_telemetry: Map<String, Value>,
        result: &mut AgentRunResult,
        projection: &mut TraceProjection,
    ) -> Result<ToolOutcome, AgentRunError> {
        let parsed = ParsedToolCall::from_value(tool_call, index);
        let mut call_payload = object(json!({
            "tool_call_id": parsed.call_id,
            "name": parsed.name,
            "input": parsed.params,
            "_timeline_type": "tool_call",
        }));
        if parsed.function_present {
            call_payload.insert("status".to_owned(), Value::String("pending".to_owned()));
            call_payload.insert(
                "arguments".to_owned(),
                Value::String(parsed.arguments.clone()),
            );
        }
        let validation = capabilities.validate(&parsed);
        let (capability_id, tool_result, stop) = match validation {
            Ok(route) => {
                call_payload.insert(
                    "capability_id".to_owned(),
                    Value::String(route.id.to_string()),
                );
                let (tool_result, stop) = self
                    .run_valid_tool(invocation, route, &parsed, position, result)
                    .await?;
                (Some(route.id.clone()), tool_result, stop)
            }
            Err((code, message)) => (
                None,
                object(json!({
                    "ok": false,
                    "error": {"code": code, "message": message},
                })),
                false,
            ),
        };
        let call_draft = tool_draft(
            "zeta.tool_call.started",
            call_payload.clone(),
            &parsed.call_id,
            assistant_event_id,
        );
        result.events.push(call_draft);
        let call_object_id = record_tool_call_trace(
            &mut result.trace,
            &call_payload,
            projection.latest_assistant.as_deref(),
        )?;
        projection
            .tool_calls
            .insert(parsed.call_id.clone(), call_object_id.clone());

        let event_id = next_event_id(self.id_source)?;
        let status = tool_result_status(&tool_result);
        let mut result_payload = object(json!({
            "tool_call_id": parsed.call_id,
            "status": status,
            "name": parsed.name,
            "result": tool_result,
            "_timeline_type": "tool_result",
        }));
        if let Some(capability_id) = capability_id {
            result_payload.insert(
                "capability_id".to_owned(),
                Value::String(capability_id.to_string()),
            );
        }
        if !model_telemetry.is_empty() {
            result_payload.insert("model_telemetry".to_owned(), Value::Object(model_telemetry));
        }
        let event_type = if result_payload
            .get("result")
            .and_then(Value::as_object)
            .and_then(|value| value.get("ok"))
            == Some(&Value::Bool(false))
        {
            "zeta.tool_call.failed"
        } else {
            "zeta.tool_call.completed"
        };
        let result_draft = tool_draft(
            event_type,
            result_payload.clone(),
            &event_id,
            assistant_event_id,
        );
        result.events.push(result_draft);
        let result_object_id =
            record_tool_result_trace(&mut result.trace, &result_payload, &call_object_id)?;
        projection
            .tool_results
            .insert(event_id.clone(), result_object_id);
        Ok(ToolOutcome {
            result_event_id: event_id,
            stop,
        })
    }

    async fn run_valid_tool(
        &mut self,
        invocation: &AgentInvocation,
        route: &Route,
        parsed: &ParsedToolCall,
        position: usize,
        result: &mut AgentRunResult,
    ) -> Result<(Map<String, Value>, bool), AgentRunError> {
        match &route.kind {
            RouteKind::External(capability) => {
                let effect = effect_identity(invocation, capability, parsed)?;
                if let Some(effect) = &effect {
                    self.record_effect(
                        EffectStatus::Planned,
                        effect,
                        capability,
                        &parsed.call_id,
                        &parsed.params,
                        None,
                    )?;
                    self.record_effect(
                        EffectStatus::Started,
                        effect,
                        capability,
                        &parsed.call_id,
                        &parsed.params,
                        None,
                    )?;
                }
                let effect_key = effect.as_ref().map(|effect| effect.key.clone());
                let invocation = CapabilityInvocation {
                    capability_id: capability.id.clone(),
                    params: parsed.params.clone(),
                    base_directory: invocation.base_directory.clone(),
                    effect_key: effect_key.clone(),
                };
                let tool_result = self.tool_executor.execute(&invocation).await;
                let tool_result = match tool_result {
                    Ok(tool_result) => validated_tool_result(tool_result, capability),
                    Err(error) => object(json!({
                        "ok": false,
                        "error": {
                            "code": "tool-crashed",
                            "message": error.to_string(),
                        },
                    })),
                };
                let tool_result = normalize_tool_result(tool_result, parsed.name.as_str());
                if let Some(effect) = &effect {
                    let status = effect_terminal_status(effect.semantics, &tool_result);
                    self.record_effect(
                        status,
                        effect,
                        capability,
                        &parsed.call_id,
                        &parsed.params,
                        Some(tool_result.clone()),
                    )?;
                }
                let stop = tool_result.get("ok") == Some(&Value::Bool(true))
                    && tool_result.get("stop") == Some(&Value::Bool(true));
                Ok((tool_result, stop))
            }
            RouteKind::Wait => {
                let Some(queue_item_id) = &invocation.source_queue_item_id else {
                    return Ok((
                        tool_error(
                            "missing-wait-source",
                            "the run does not have a source queue item",
                        ),
                        false,
                    ));
                };
                let handle = control_handle("wait_", queue_item_id, position);
                let event_type = text_param(&parsed.params, "event_type");
                let fields = parsed
                    .params
                    .get("fields")
                    .and_then(Value::as_object)
                    .cloned()
                    .unwrap_or_default();
                let deadline = optional_text_param(&parsed.params, "deadline");
                result.requests.push(AgentRequest::Wait {
                    handle: handle.clone(),
                    event_type,
                    fields,
                    deadline,
                    position,
                });
                Ok((
                    object(json!({"ok": true, "handle": handle, "stop": true})),
                    true,
                ))
            }
            RouteKind::Publish => {
                let Some(queue_item_id) = &invocation.source_queue_item_id else {
                    return Ok((
                        tool_error(
                            "missing-publish-source",
                            "the run does not have a source queue item",
                        ),
                        false,
                    ));
                };
                let event_type = text_param(&parsed.params, "event_type");
                if !invocation.publishable_events.contains_key(&event_type) {
                    return Ok((
                        tool_error(
                            "undeclared-event-type",
                            &format!("the agent does not list event '{event_type}' in publishes"),
                        ),
                        false,
                    ));
                }
                let payload = parsed
                    .params
                    .get("payload")
                    .and_then(Value::as_object)
                    .cloned()
                    .unwrap_or_default();
                let schema = &invocation.publishable_events[&event_type];
                if let Some(schema) = schema.as_object() {
                    if let Some(violation) =
                        schema_violation(&Value::Object(payload.clone()), schema)
                    {
                        return Ok((
                            tool_error("invalid-event-payload", &violation.message),
                            false,
                        ));
                    }
                }
                let at = optional_text_param(&parsed.params, "at");
                let handle = control_handle("pub_", queue_item_id, position);
                result.requests.push(AgentRequest::Publish {
                    handle: handle.clone(),
                    event_type,
                    payload,
                    at,
                    position,
                });
                Ok((object(json!({"ok": true, "handle": handle})), false))
            }
            RouteKind::Cancel => {
                let Some(source_agent_id) = &invocation.source_agent_id else {
                    return Ok((
                        tool_error(
                            "missing-cancel-source",
                            "the run does not have an authored agent session",
                        ),
                        false,
                    ));
                };
                let Some(source_session_id) = &invocation.source_session_id else {
                    return Ok((
                        tool_error(
                            "missing-cancel-source",
                            "the run does not have an authored agent session",
                        ),
                        false,
                    ));
                };
                let handle = text_param(&parsed.params, "handle");
                result.requests.push(AgentRequest::Cancel {
                    handle: handle.clone(),
                    reason: optional_text_param(&parsed.params, "reason"),
                    source_agent_id: source_agent_id.clone(),
                    source_session_id: source_session_id.clone(),
                    position,
                });
                Ok((
                    object(json!({"ok": true, "handle": handle, "status": "requested"})),
                    false,
                ))
            }
            RouteKind::Return => {
                let event_type = text_param(&parsed.params, "event_type");
                if !invocation.returnable_events.contains_key(&event_type) {
                    return Ok((
                        tool_error(
                            "event-not-returnable",
                            "the event type is not returnable by this invocation",
                        ),
                        false,
                    ));
                }
                let payload = parsed
                    .params
                    .get("payload")
                    .and_then(Value::as_object)
                    .cloned()
                    .unwrap_or_default();
                result.requests.push(AgentRequest::Return {
                    event_type,
                    payload,
                    position,
                });
                Ok((object(json!({"ok": true, "stop": true})), true))
            }
        }
    }

    fn record_effect(
        &mut self,
        status: EffectStatus,
        identity: &EffectIdentity,
        capability: &Capability,
        caused_by: &str,
        params: &Map<String, Value>,
        result: Option<Map<String, Value>>,
    ) -> Result<(), AgentRunError> {
        self.effect_recorder
            .record(EffectEvent {
                status,
                effect_key: identity.key.clone(),
                capability_id: capability.id.clone(),
                semantics: identity.semantics,
                scope: identity.scope.clone(),
                caused_by: caused_by.to_owned(),
                params: params.clone(),
                result,
            })
            .map_err(AgentRunError::Failed)
    }
}

struct RunAbort<'a> {
    external: &'a dyn AbortSignal,
    clock: &'a dyn Clock,
    deadline_ms: Option<i64>,
}

impl AbortSignal for RunAbort<'_> {
    fn reason(&self) -> Option<AbortReason> {
        if let Some(reason) = self.external.reason() {
            return Some(reason);
        }
        let deadline_ms = self.deadline_ms?;
        if self.clock.now_millis() >= deadline_ms {
            Some(AbortReason::DeadlineExceeded)
        } else {
            None
        }
    }
}

#[derive(Default)]
struct RunState {
    pending_tool_calls: Vec<Value>,
    pending_model_telemetry: Map<String, Value>,
    pending_tool_parent_id: Option<String>,
    next_model_caused_by: Option<String>,
    model_calls: usize,
    next_tool_position: usize,
    projection: TraceProjection,
}

#[derive(Default)]
struct TraceProjection {
    models: HashMap<String, (String, String)>,
    tool_calls: HashMap<String, String>,
    tool_results: HashMap<String, String>,
    latest_assistant: Option<String>,
}

struct CapabilitySet {
    routes: HashMap<String, Route>,
    declared_names: HashMap<String, usize>,
    declared_ids: HashSet<String>,
    descriptors: Vec<Value>,
    allowed_ids: Vec<CapabilityId>,
}

struct Route {
    id: CapabilityId,
    schema: Map<String, Value>,
    kind: RouteKind,
}

enum RouteKind {
    External(Capability),
    Publish,
    Wait,
    Cancel,
    Return,
}

impl CapabilitySet {
    fn new(invocation: &AgentInvocation, declarations: &[Capability]) -> Result<Self, AgentError> {
        let mut by_id = HashMap::new();
        let mut declared_names = HashMap::new();
        let mut declared_ids = HashSet::new();
        for declaration in declarations {
            if by_id.insert(declaration.id.as_str(), declaration).is_some() {
                return Err(AgentError::invocation(format!(
                    "duplicate capability id: {}",
                    declaration.id
                )));
            }
            let name = declaration.id.model_name().to_owned();
            let count = declared_names.entry(name).or_insert(0);
            *count += 1;
            declared_ids.insert(declaration.id.to_string());
        }
        let mut set = CapabilitySet {
            routes: HashMap::new(),
            declared_names,
            declared_ids,
            descriptors: Vec::new(),
            allowed_ids: Vec::new(),
        };
        for id in &invocation.allowed_capabilities {
            let Some(declaration) = by_id.get(id.as_str()) else {
                return Err(AgentError::invocation(format!(
                    "unknown capability grant: {id}"
                )));
            };
            set.add_route(
                declaration.id.model_name(),
                Route {
                    id: declaration.id.clone(),
                    schema: declaration.input_schema.clone(),
                    kind: RouteKind::External((*declaration).clone()),
                },
                declaration.description.as_str(),
            )?;
        }
        if !invocation.publishable_events.is_empty() && invocation.source_queue_item_id.is_some() {
            set.add_internal(
                "publish_event",
                "zeta.publish_event",
                "Request an event when this agent attempt completes successfully.",
                object(json!({
                    "type": "object",
                    "required": ["event_type", "payload"],
                    "properties": {
                        "event_type": {"type": "string"},
                        "payload": {"type": "object"},
                        "at": {"type": "string"},
                    },
                    "additionalProperties": false,
                })),
                RouteKind::Publish,
            )?;
        }
        if invocation.source_queue_item_id.is_some() {
            set.add_internal(
                "wait_for",
                "zeta.wait_for",
                "End this run and resume when a matching event arrives.",
                object(json!({
                    "type": "object",
                    "required": ["event_type"],
                    "properties": {
                        "event_type": {"type": "string", "minLength": 1},
                        "fields": {"type": "object"},
                        "deadline": {"type": "string"},
                    },
                    "additionalProperties": false,
                })),
                RouteKind::Wait,
            )?;
        }
        if invocation.source_queue_item_id.is_some()
            && invocation.source_agent_id.is_some()
            && invocation.source_session_id.is_some()
        {
            set.add_internal(
                "cancel",
                "zeta.cancel",
                "Cancel an active wait or pending scheduled event from this session.",
                object(json!({
                    "type": "object",
                    "required": ["handle"],
                    "properties": {
                        "handle": {"type": "string", "pattern": "^(?:wait|pub)_.+$"},
                        "reason": {"type": "string", "minLength": 1},
                    },
                    "additionalProperties": false,
                })),
                RouteKind::Cancel,
            )?;
        }
        if !invocation.returnable_events.is_empty() {
            set.add_internal(
                "return_event",
                "zeta.return_event",
                "Return a typed event to the caller and end this run.",
                object(json!({
                    "type": "object",
                    "required": ["event_type", "payload"],
                    "properties": {
                        "event_type": {"type": "string"},
                        "payload": {"type": "object"},
                    },
                    "additionalProperties": false,
                })),
                RouteKind::Return,
            )?;
        }
        Ok(set)
    }

    fn add_internal(
        &mut self,
        name: &str,
        id: &str,
        description: &str,
        schema: Map<String, Value>,
        kind: RouteKind,
    ) -> Result<(), AgentError> {
        let id = id.parse::<CapabilityId>()?;
        self.add_route(name, Route { id, schema, kind }, description)
    }

    fn add_route(&mut self, name: &str, route: Route, description: &str) -> Result<(), AgentError> {
        if self.routes.contains_key(name) {
            return Err(AgentError::invocation(format!(
                "reserved or ambiguous capability name: {name}"
            )));
        }
        self.allowed_ids.push(route.id.clone());
        self.descriptors.push(json!({
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": route.schema,
            },
        }));
        self.routes.insert(name.to_owned(), route);
        Ok(())
    }

    fn validate(&self, parsed: &ParsedToolCall) -> Result<&Route, (String, String)> {
        if let Some(parse_error) = &parsed.parse_error {
            return Err((parsed.parse_error_code.to_owned(), parse_error.clone()));
        }
        let Some(route) = self.routes.get(parsed.name.as_str()) else {
            let known_name = self.declared_names.get(parsed.name.as_str()) == Some(&1);
            if known_name || self.declared_ids.contains(parsed.name.as_str()) {
                return Err((
                    "disallowed-tool".to_owned(),
                    format!("tool is not allowed for this run: {}", parsed.name),
                ));
            }
            return Err((
                "unknown-tool".to_owned(),
                format!("unknown tool: {}", parsed.name),
            ));
        };
        if let Some(message) = schema_error(&Value::Object(parsed.params.clone()), &route.schema) {
            return Err((
                "invalid-tool-args".to_owned(),
                format!("model arguments: {message}"),
            ));
        }
        Ok(route)
    }
}

struct ParsedToolCall {
    call_id: String,
    name: String,
    arguments: String,
    params: Map<String, Value>,
    parse_error: Option<String>,
    parse_error_code: &'static str,
    function_present: bool,
}

impl ParsedToolCall {
    fn from_value(value: Value, index: usize) -> Self {
        let call_id = value.get("id").and_then(Value::as_str).unwrap_or("");
        let call_id = if call_id.is_empty() {
            format!("call-{index}")
        } else {
            call_id.to_owned()
        };
        let Some(function) = value.get("function").and_then(Value::as_object) else {
            return ParsedToolCall {
                call_id,
                name: String::new(),
                arguments: "{}".to_owned(),
                params: Map::new(),
                parse_error: Some("tool call did not include a function payload".to_owned()),
                parse_error_code: "invalid-tool-call",
                function_present: false,
            };
        };
        let name = function
            .get("name")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_owned();
        let Some(arguments) = function.get("arguments").and_then(Value::as_str) else {
            return ParsedToolCall {
                call_id,
                name,
                arguments: "{}".to_owned(),
                params: Map::new(),
                parse_error: Some("function arguments were not a JSON object string".to_owned()),
                parse_error_code: "invalid-json-args",
                function_present: true,
            };
        };
        let value = serde_json::from_str::<Value>(arguments);
        match value {
            Ok(Value::Object(params)) => ParsedToolCall {
                call_id,
                name,
                arguments: arguments.to_owned(),
                params,
                parse_error: None,
                parse_error_code: "invalid-json-args",
                function_present: true,
            },
            Ok(Value::Null) => invalid_arguments(call_id, name, arguments),
            Ok(Value::Bool(_value)) => invalid_arguments(call_id, name, arguments),
            Ok(Value::Number(_value)) => invalid_arguments(call_id, name, arguments),
            Ok(Value::String(_value)) => invalid_arguments(call_id, name, arguments),
            Ok(Value::Array(_value)) => invalid_arguments(call_id, name, arguments),
            Err(error) => ParsedToolCall {
                call_id,
                name,
                arguments: arguments.to_owned(),
                params: Map::new(),
                parse_error: Some(error.to_string()),
                parse_error_code: "invalid-json-args",
                function_present: true,
            },
        }
    }
}

struct ToolOutcome {
    result_event_id: String,
    stop: bool,
}

struct EffectIdentity {
    key: String,
    scope: String,
    semantics: DeliverySemantics,
}

fn invalid_arguments(call_id: String, name: String, arguments: &str) -> ParsedToolCall {
    ParsedToolCall {
        call_id,
        name,
        arguments: arguments.to_owned(),
        params: Map::new(),
        parse_error: Some("function arguments JSON was not an object".to_owned()),
        parse_error_code: "invalid-json-args",
        function_present: true,
    }
}

fn abort_run(
    mut result: AgentRunResult,
    reason: AbortReason,
    caused_by: Option<String>,
) -> Result<AgentRunResult, AgentRunError> {
    result.steps.push(StepName::AbortRun);
    result.events.push(DraftEvent {
        event_type: "zeta.turn.failed".to_owned(),
        source: "zeta".to_owned(),
        payload: object(json!({
            "_timeline_type": "turn_aborted",
            "reason": reason.to_string(),
            "content": format!("(turn aborted: {})", reason.to_string().replace('_', " ")),
        })),
        idempotency_key: None,
        caused_by,
        session_id: None,
        run_id: None,
        turn_id: None,
    });
    Err(AgentRunError::Aborted(Box::new(AgentRunAborted {
        reason,
        result,
    })))
}

fn next_event_id(source: &mut dyn IdSource) -> Result<String, AgentError> {
    let id = source.next_id()?;
    if id.is_empty() {
        return Err(AgentError::identity("event id must not be empty"));
    }
    Ok(id)
}

fn model_payload(message: &Map<String, Value>, prompt_object_id: &str) -> Map<String, Value> {
    let mut payload = Map::new();
    if let Some(reasoning) = message.get("reasoning_content").and_then(Value::as_str) {
        if !reasoning.is_empty() {
            payload.insert("reasoning".to_owned(), Value::String(reasoning.to_owned()));
        }
    }
    if let Some(content) = message.get("content").and_then(Value::as_str) {
        if !content.is_empty() {
            payload.insert("content".to_owned(), Value::String(content.to_owned()));
        }
    }
    let tool_calls = assistant_tool_calls(message);
    if !tool_calls.is_empty() {
        payload.insert("tool_calls".to_owned(), Value::Array(tool_calls));
    }
    payload.insert(
        "prompt_object_id".to_owned(),
        Value::String(prompt_object_id.to_owned()),
    );
    payload.insert(
        "_timeline_type".to_owned(),
        Value::String("model".to_owned()),
    );
    payload
}

fn assistant_tool_calls(message: &Map<String, Value>) -> Vec<Value> {
    let mut calls = Vec::new();
    let Some(values) = message.get("tool_calls").and_then(Value::as_array) else {
        return calls;
    };
    for value in values {
        if value.is_object() {
            calls.push(value.clone());
        }
    }
    calls
}

fn model_draft(
    payload: Map<String, Value>,
    event_id: &str,
    caused_by: Option<String>,
) -> DraftEvent {
    DraftEvent {
        event_type: "zeta.model_call.completed".to_owned(),
        source: "zeta".to_owned(),
        payload,
        idempotency_key: Some(format!("zeta.model_call.completed:{event_id}")),
        caused_by,
        session_id: None,
        run_id: None,
        turn_id: None,
    }
}

fn tool_draft(
    event_type: &str,
    payload: Map<String, Value>,
    event_id: &str,
    caused_by: Option<&str>,
) -> DraftEvent {
    DraftEvent {
        event_type: event_type.to_owned(),
        source: "zeta".to_owned(),
        payload,
        idempotency_key: Some(format!("{event_type}:{event_id}")),
        caused_by: caused_by.map(str::to_owned),
        session_id: None,
        run_id: None,
        turn_id: None,
    }
}

fn record_model_trace(
    trace: &mut TraceBatch,
    prompt_object_id: &str,
    payload: &Map<String, Value>,
) -> Result<String, AgentError> {
    let mut message = Map::new();
    if let Some(content) = payload.get("content") {
        message.insert("content".to_owned(), content.clone());
    }
    if let Some(reasoning) = payload.get("reasoning") {
        message.insert("reasoning_content".to_owned(), reasoning.clone());
    }
    if let Some(tool_calls) = payload.get("tool_calls") {
        message.insert("tool_calls".to_owned(), tool_calls.clone());
    }
    let assistant_id = trace.insert_object(Object {
        kind: "assistant_message".to_owned(),
        schema: "zeta.model_output.v1".to_owned(),
        data: object(json!({
            "message": message,
            "model_output": {"message": message},
        })),
        links: vec![prompt_object_id.to_owned()],
    })?;
    trace.insert_derivation(Derivation {
        producer: "ModelResponse".to_owned(),
        output_id: assistant_id.clone(),
        input_ids: vec![prompt_object_id.to_owned()],
        params: Map::new(),
    })?;
    Ok(assistant_id)
}

fn record_tool_call_trace(
    trace: &mut TraceBatch,
    payload: &Map<String, Value>,
    assistant_object_id: Option<&str>,
) -> Result<String, AgentError> {
    let Some(assistant_object_id) = assistant_object_id else {
        return Err(AgentError::trace(
            "tool call trace is missing its assistant source",
        ));
    };
    let call_id = text_value(payload, "tool_call_id");
    let name = text_value(payload, "name");
    let input = payload
        .get("input")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    let mut data = object(json!({
        "tool_call_id": call_id,
        "name": name,
        "input": input,
    }));
    if let Some(arguments) = payload.get("arguments").and_then(Value::as_str) {
        data.insert("arguments".to_owned(), Value::String(arguments.to_owned()));
    }
    let call_object_id = trace.insert_object(Object {
        kind: "tool_call".to_owned(),
        schema: "zeta.tool_call.v1".to_owned(),
        data,
        links: vec![assistant_object_id.to_owned()],
    })?;
    trace.insert_derivation(Derivation {
        producer: "ToolCallProjection".to_owned(),
        output_id: call_object_id.clone(),
        input_ids: vec![assistant_object_id.to_owned()],
        params: object(json!({"tool_call_id": call_id, "name": name})),
    })?;
    Ok(call_object_id)
}

fn record_tool_result_trace(
    trace: &mut TraceBatch,
    payload: &Map<String, Value>,
    call_object_id: &str,
) -> Result<String, AgentError> {
    let call_id = text_value(payload, "tool_call_id");
    let name = text_value(payload, "name");
    let mut data = object(json!({"tool_call_id": call_id, "name": name}));
    if let Some(tool_result) = payload.get("result") {
        data.insert("result".to_owned(), tool_result.clone());
    }
    if let Some(telemetry) = payload.get("model_telemetry") {
        data.insert("model_telemetry".to_owned(), telemetry.clone());
    }
    let result_object_id = trace.insert_object(Object {
        kind: "tool_result".to_owned(),
        schema: "zeta.tool_result.v1".to_owned(),
        data,
        links: vec![call_object_id.to_owned()],
    })?;
    trace.insert_derivation(Derivation {
        producer: "ToolExecution".to_owned(),
        output_id: result_object_id.clone(),
        input_ids: vec![call_object_id.to_owned()],
        params: object(json!({"tool_call_id": call_id, "name": name})),
    })?;
    Ok(result_object_id)
}

fn current_event_views(
    drafts: &[DraftEvent],
    projection: &TraceProjection,
) -> Vec<Map<String, Value>> {
    let mut views = Vec::new();
    for draft in drafts {
        let timeline_type = draft
            .payload
            .get("_timeline_type")
            .and_then(Value::as_str)
            .unwrap_or(draft.event_type.as_str());
        let mut view = Map::new();
        view.insert("type".to_owned(), Value::String(timeline_type.to_owned()));
        let event_id = draft_event_id(draft);
        if let Some(event_id) = &event_id {
            view.insert("id".to_owned(), Value::String(event_id.clone()));
        }
        if let Some(caused_by) = &draft.caused_by {
            view.insert("caused_by".to_owned(), Value::String(caused_by.clone()));
        }
        for (key, value) in &draft.payload {
            if key != "_timeline_type" {
                view.insert(key.clone(), value.clone());
            }
        }
        if let Some(event_id) = event_id {
            if timeline_type == "model" {
                if let Some((prompt_id, assistant_id)) = projection.models.get(&event_id) {
                    view.insert(
                        "prompt_trace".to_owned(),
                        json!({
                            "prompt_object_id": prompt_id,
                            "assistant_message_object_id": assistant_id,
                        }),
                    );
                }
            } else if timeline_type == "tool_call" {
                if let Some(call_id) = projection.tool_calls.get(&event_id) {
                    view.insert(
                        "tool_call_object_id".to_owned(),
                        Value::String(call_id.clone()),
                    );
                }
            } else if timeline_type == "tool_result" {
                let call_id = view
                    .get("tool_call_id")
                    .and_then(Value::as_str)
                    .unwrap_or("");
                if let Some(call_object_id) = projection.tool_calls.get(call_id) {
                    view.insert(
                        "tool_call_object_id".to_owned(),
                        Value::String(call_object_id.clone()),
                    );
                }
                if let Some(result_object_id) = projection.tool_results.get(&event_id) {
                    view.insert(
                        "tool_result_object_id".to_owned(),
                        Value::String(result_object_id.clone()),
                    );
                }
            }
        }
        views.push(view);
    }
    views
}

fn draft_event_id(draft: &DraftEvent) -> Option<String> {
    let key = draft.idempotency_key.as_deref()?;
    let prefix = format!("{}:", draft.event_type);
    let id = key.strip_prefix(&prefix)?.trim();
    if id.is_empty() {
        None
    } else {
        Some(id.to_owned())
    }
}

struct SchemaViolation {
    location: String,
    message: String,
}

fn schema_error(value: &Value, schema: &Map<String, Value>) -> Option<String> {
    let violation = schema_violation(value, schema)?;
    if violation.location.is_empty() {
        Some(violation.message)
    } else {
        Some(format!(
            "{}: {}",
            violation.location.trim_start_matches('/'),
            violation.message,
        ))
    }
}

fn schema_violation(value: &Value, schema: &Map<String, Value>) -> Option<SchemaViolation> {
    if schema.is_empty() {
        return None;
    }
    let schema = Value::Object(schema.clone());
    if !jsonschema::draft202012::meta::is_valid(&schema) {
        return None;
    }
    let validator = match jsonschema::draft202012::new(&schema) {
        Ok(validator) => validator,
        Err(_error) => return None,
    };
    let mut first: Option<SchemaViolation> = None;
    for error in validator.iter_errors(value) {
        let location = error.instance_path.to_string();
        let violation = SchemaViolation {
            location,
            message: error.to_string(),
        };
        let replace = match &first {
            Some(first) => violation.location < first.location,
            None => true,
        };
        if replace {
            first = Some(violation);
        }
    }
    first
}

fn tool_result_status(result: &Map<String, Value>) -> &'static str {
    if result.get("ok") == Some(&Value::Bool(true)) {
        return "completed";
    }
    let code = result
        .get("error")
        .and_then(Value::as_object)
        .and_then(|error| error.get("code"))
        .and_then(Value::as_str)
        .unwrap_or("");
    if is_refused_code(code) {
        "refused"
    } else {
        "failed"
    }
}

fn is_refused_code(code: &str) -> bool {
    code == "direct-execution-disallowed"
        || code == "disallowed-tool"
        || code == "invalid-json-args"
        || code == "invalid-tool-call"
        || code == "schema-mismatch"
        || code == "staging-unsupported"
        || code == "unknown-tool"
}

fn validated_tool_result(
    mut result: Map<String, Value>,
    capability: &Capability,
) -> Map<String, Value> {
    if result.get("ok").and_then(Value::as_bool).is_some() {
        if result.get("ok") == Some(&Value::Bool(false))
            && result.get("error").and_then(Value::as_object).is_none()
        {
            result.insert(
                "error".to_owned(),
                json!({
                    "code": "invalid-capability-result",
                    "message": format!("capability {} returned an invalid result", capability.id),
                }),
            );
        }
        return result;
    }
    result.insert("ok".to_owned(), Value::Bool(false));
    result.insert(
        "error".to_owned(),
        json!({
            "code": "invalid-capability-result",
            "message": format!("capability {} returned an invalid result", capability.id),
        }),
    );
    result
}

fn normalize_tool_result(mut result: Map<String, Value>, name: &str) -> Map<String, Value> {
    if result.get("ok") != Some(&Value::Bool(false))
        || result.get("error").and_then(Value::as_object).is_some()
    {
        return result;
    }
    let message = first_tool_text(&result);
    if !message.is_empty() {
        let name = if name.is_empty() { "tool" } else { name };
        result.insert(
            "error".to_owned(),
            json!({
                "code": format!("{name}-failed"),
                "message": flatten_whitespace(&message),
            }),
        );
    }
    result
}

fn first_tool_text(result: &Map<String, Value>) -> String {
    let Some(content) = result.get("content").and_then(Value::as_array) else {
        return String::new();
    };
    for item in content {
        let Some(text) = item
            .as_object()
            .and_then(|item| item.get("text"))
            .and_then(Value::as_str)
        else {
            continue;
        };
        if !text.trim().is_empty() {
            return text.to_owned();
        }
    }
    String::new()
}

fn flatten_whitespace(value: &str) -> String {
    let mut flattened = String::new();
    for word in value.split_whitespace() {
        if !flattened.is_empty() {
            flattened.push(' ');
        }
        flattened.push_str(word);
    }
    flattened
}

fn effect_identity(
    invocation: &AgentInvocation,
    capability: &Capability,
    parsed: &ParsedToolCall,
) -> Result<Option<EffectIdentity>, AgentError> {
    let Some(semantics) = capability.delivery_semantics else {
        return Ok(None);
    };
    let scope = invocation
        .effect_scope
        .as_deref()
        .unwrap_or(parsed.call_id.as_str());
    let value = json!({
        "scope": scope,
        "operation": capability.id.as_str(),
        "params": parsed.params,
    });
    let bytes = canonical_json(&value).map_err(|error| AgentError::effect(error.to_string()))?;
    Ok(Some(EffectIdentity {
        key: format!("effect:{}", derive(Domain::Chain, &bytes)),
        scope: scope.to_owned(),
        semantics,
    }))
}

fn effect_terminal_status(
    semantics: DeliverySemantics,
    result: &Map<String, Value>,
) -> EffectStatus {
    if result.get("ok") == Some(&Value::Bool(true)) {
        return EffectStatus::Completed;
    }
    if semantics == DeliverySemantics::UnsafeToRetry {
        EffectStatus::Ambiguous
    } else {
        EffectStatus::Failed
    }
}

fn control_handle(prefix: &str, queue_item_id: &str, position: usize) -> String {
    let identity = format!("{queue_item_id}:{position}");
    format!("{prefix}{}", derive(Domain::Chain, identity.as_bytes()))
}

fn tool_error(code: &str, message: &str) -> Map<String, Value> {
    object(json!({"ok": false, "error": {"code": code, "message": message}}))
}

fn text_param(params: &Map<String, Value>, key: &str) -> String {
    params
        .get(key)
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_owned()
}

fn optional_text_param(params: &Map<String, Value>, key: &str) -> Option<String> {
    params.get(key).and_then(Value::as_str).map(str::to_owned)
}

fn text_value(value: &Map<String, Value>, key: &str) -> String {
    value
        .get(key)
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_owned()
}

fn object(value: Value) -> Map<String, Value> {
    value
        .as_object()
        .expect("internal runner JSON value is an object")
        .clone()
}
