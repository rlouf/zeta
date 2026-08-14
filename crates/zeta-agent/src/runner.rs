//! Model and capability state machine for one resolved invocation.

use std::collections::{HashMap, HashSet};

use jsonschema::error::ValidationErrorKind;
use serde_json::{json, Map, Value};
use time::format_description::well_known::Rfc3339;
use time::{OffsetDateTime, UtcOffset};
use zeta_journal::DraftEvent;
use zeta_substrate::{canonical_json, derive, Derivation, Domain, Object};

use crate::capability::{
    ArgumentAdapter, Capability, CapabilityExecutor, CapabilityId, CapabilityInvocation,
    DeliverySemantics, DraftRecorder, IdSource, ResolvedCapability,
};
use crate::control::AgentProposal;
use crate::error::{AgentError, AgentRunAborted, AgentRunError};
use crate::invocation::AgentInvocation;
use crate::model::{AbortReason, AbortSignal, AgentObserver, Clock, ModelGateway, ModelRequest};
use crate::prompt::{build_prompt, PromptBuild, PromptInput, PromptTransform};
use crate::result::{AgentRunResult, RunStopReason, StepName};
use crate::trace::{PromptTrace, TraceBatch};

/// Owns borrowed runtime services for one provider-neutral invocation.
pub struct AgentRunner<'a> {
    capabilities: &'a [ResolvedCapability],
    model_gateway: &'a mut dyn ModelGateway,
    tool_executor: &'a mut dyn CapabilityExecutor,
    observer: &'a mut dyn AgentObserver,
    draft_recorder: &'a mut dyn DraftRecorder,
    id_source: &'a mut dyn IdSource,
    abort: &'a dyn AbortSignal,
    clock: &'a dyn Clock,
}

impl<'a> AgentRunner<'a> {
    /// Creates a runner around caller-owned runtime services.
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        capabilities: &'a [ResolvedCapability],
        model_gateway: &'a mut dyn ModelGateway,
        tool_executor: &'a mut dyn CapabilityExecutor,
        observer: &'a mut dyn AgentObserver,
        draft_recorder: &'a mut dyn DraftRecorder,
        id_source: &'a mut dyn IdSource,
        abort: &'a dyn AbortSignal,
        clock: &'a dyn Clock,
    ) -> Self {
        AgentRunner {
            capabilities,
            model_gateway,
            tool_executor,
            observer,
            draft_recorder,
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
                let control = self
                    .run_pending_tool_batch(invocation, &capabilities, &mut result, &mut state)
                    .await?;
                match control {
                    RunLoopControl::Continue => continue,
                    RunLoopControl::Finish => return Ok(result),
                    RunLoopControl::Abort { reason, caused_by } => {
                        return self.abort_run(invocation, result, reason, caused_by);
                    }
                }
            }
            if state.model_calls >= invocation.max_model_calls {
                result.stop_reason = Some(RunStopReason::MaxModelCalls);
                result.steps.push(StepName::FinishRun);
                return Ok(result);
            }
            result.steps.push(StepName::CheckAbort);
            if let Some(reason) = self.abort_reason(invocation) {
                return self.abort_run(invocation, result, reason, state.next_model_caused_by);
            }
            let control = self
                .run_model_turn(invocation, &capabilities, &mut result, &mut state)
                .await?;
            match control {
                RunLoopControl::Continue => {}
                RunLoopControl::Finish => return Ok(result),
                RunLoopControl::Abort { reason, caused_by } => {
                    return self.abort_run(invocation, result, reason, caused_by);
                }
            }
        }
    }

    async fn run_pending_tool_batch(
        &mut self,
        invocation: &AgentInvocation,
        capabilities: &CapabilitySet,
        result: &mut AgentRunResult,
        state: &mut RunState,
    ) -> Result<RunLoopControl, AgentRunError> {
        let tool_calls = std::mem::take(&mut state.pending_tool_calls);
        let model_telemetry = std::mem::take(&mut state.pending_model_telemetry);
        let assistant_event_id = state.pending_tool_parent_id.take();
        for (index, tool_call) in tool_calls.into_iter().enumerate() {
            result.steps.push(StepName::CheckAbort);
            if let Some(reason) = self.abort_reason(invocation) {
                return Ok(RunLoopControl::Abort {
                    reason,
                    caused_by: state.next_model_caused_by.clone(),
                });
            }
            let position = state.next_tool_position;
            state.next_tool_position += 1;
            let call_id = tool_call_id(&tool_call, index);
            if terminal_tool_result(&result.events, &call_id).is_some() {
                result.steps.push(StepName::RecordCapabilityResult);
                state.next_model_caused_by = None;
                continue;
            }
            result.steps.push(StepName::RecordCapabilityCall);
            result.steps.push(StepName::ExecuteCapability);
            let telemetry = if index == 0 {
                model_telemetry.clone()
            } else {
                Map::new()
            };
            let outcome = self
                .process_tool_call(
                    invocation,
                    capabilities,
                    tool_call,
                    index,
                    position,
                    assistant_event_id.as_deref(),
                    telemetry,
                    result,
                    &mut state.projection,
                )
                .await?;
            result.steps.push(StepName::RecordCapabilityResult);
            state.next_model_caused_by = Some(outcome.result_event_id.clone());
            if let Some(reason) = self.abort_reason(invocation) {
                return Ok(RunLoopControl::Abort {
                    reason,
                    caused_by: Some(outcome.result_event_id),
                });
            }
            if outcome.stop {
                result.stop_reason = Some(RunStopReason::ToolStop);
                result.steps.push(StepName::FinishRun);
                return Ok(RunLoopControl::Finish);
            }
        }
        Ok(RunLoopControl::Continue)
    }

    async fn run_model_turn(
        &mut self,
        invocation: &AgentInvocation,
        capabilities: &CapabilitySet,
        result: &mut AgentRunResult,
        state: &mut RunState,
    ) -> Result<RunLoopControl, AgentRunError> {
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
                content_components: Vec::new(),
                transform: invocation.prompt_transform.clone(),
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
        self.record_trace(result)?;
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
                    return Ok(RunLoopControl::Abort {
                        reason,
                        caused_by: state.next_model_caused_by.clone(),
                    });
                }
                return Err(error.into());
            }
        };
        result.steps.push(StepName::RecordAssistant);
        if !output.telemetry.is_empty() {
            result.telemetry = output.telemetry.clone();
            result.model_telemetry_calls.push(output.telemetry.clone());
        }
        result.prompt_traces.push(PromptTrace {
            prompt_object_id: prompt_object_id.clone(),
            assistant_message_object_id: None,
        });
        if self.abort.reason() == Some(AbortReason::Cancelled) {
            return Ok(RunLoopControl::Abort {
                reason: AbortReason::Cancelled,
                caused_by: state.next_model_caused_by.clone(),
            });
        }
        let event_id = next_event_id(self.id_source)?;
        let model_payload = model_payload(&output.message, &prompt_object_id);
        let assistant_object_id =
            record_model_trace(&mut result.trace, &prompt_object_id, &model_payload)?;
        self.record_trace(result)?;
        let draft = model_draft(
            invocation,
            model_payload.clone(),
            &event_id,
            state.next_model_caused_by.clone(),
        );
        let event_id = self.record_durable_draft(result, &event_id, draft)?;
        state.projection.models.insert(
            event_id.clone(),
            (prompt_object_id.clone(), assistant_object_id.clone()),
        );
        state.projection.latest_assistant = Some(assistant_object_id.clone());
        let Some(prompt_trace) = result.prompt_traces.last_mut() else {
            return Err(AgentError::trace("model response is missing its prompt trace").into());
        };
        prompt_trace.assistant_message_object_id = Some(assistant_object_id);
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
            return Ok(RunLoopControl::Finish);
        }
        state.pending_tool_calls = tool_calls;
        state.pending_model_telemetry = output.telemetry;
        state.pending_tool_parent_id = Some(event_id);
        Ok(RunLoopControl::Continue)
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
    async fn process_tool_call(
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
        if let Ok(validated) = &validation {
            call_payload.insert(
                "capability_id".to_owned(),
                Value::String(validated.route.id.to_string()),
            );
        }
        let call_object_id = record_tool_call_trace(
            &mut result.trace,
            &call_payload,
            projection.latest_assistant.as_deref(),
        )?;
        self.record_trace(result)?;
        let call_event_id = parsed.call_id.clone();
        let call_draft = tool_draft(
            invocation,
            "zeta.tool_call.started",
            call_payload.clone(),
            &call_event_id,
            assistant_event_id,
        );
        let call_event_id = self.record_durable_draft(result, &call_event_id, call_draft)?;
        projection
            .tool_calls
            .insert(parsed.call_id.clone(), call_object_id.clone());

        let execution = match validation {
            Ok(validated) => {
                self.run_valid_tool(
                    invocation,
                    validated.route,
                    &parsed,
                    &validated.canonical_params,
                    position,
                    &call_event_id,
                    result,
                )
                .await?
            }
            Err((code, message)) => ToolExecution {
                capability_id: None,
                tool_result: object(json!({
                    "ok": false,
                    "error": {"code": code, "message": message},
                })),
                stop: false,
                effect: None,
            },
        };

        let event_id = next_event_id(self.id_source)?;
        let status = tool_result_status(&execution.tool_result);
        let mut result_payload = object(json!({
            "tool_call_id": parsed.call_id,
            "status": status,
            "name": parsed.name,
            "result": execution.tool_result,
            "_timeline_type": "tool_result",
        }));
        if let Some(capability_id) = execution.capability_id {
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
        let result_object_id =
            record_tool_result_trace(&mut result.trace, &result_payload, &call_object_id)?;
        self.record_trace(result)?;
        let result_draft = tool_draft(
            invocation,
            event_type,
            result_payload.clone(),
            &event_id,
            assistant_event_id,
        );
        let event_id = self.record_durable_draft(result, &event_id, result_draft)?;
        projection
            .tool_results
            .insert(event_id.clone(), result_object_id);
        if let Some(effect) = execution.effect {
            let tool_result = result_payload
                .get("result")
                .and_then(Value::as_object)
                .cloned()
                .unwrap_or_default();
            let status = effect_terminal_status(effect.identity.semantics, &tool_result);
            self.record_effect(
                invocation,
                result,
                EffectRecord {
                    status,
                    identity: &effect.identity,
                    capability: &effect.capability,
                    caused_by: &call_event_id,
                    params: &effect.params,
                    result: Some(tool_result),
                },
            )?;
        }
        Ok(ToolOutcome {
            result_event_id: event_id,
            stop: execution.stop,
        })
    }

    async fn run_valid_tool(
        &mut self,
        invocation: &AgentInvocation,
        route: &Route,
        parsed: &ParsedToolCall,
        canonical_params: &Map<String, Value>,
        position: usize,
        call_event_id: &str,
        result: &mut AgentRunResult,
    ) -> Result<ToolExecution, AgentRunError> {
        match &route.kind {
            RouteKind::External(capability) => {
                let capability = &capability.canonical;
                let effect = effect_identity(invocation, capability, parsed, canonical_params)?;
                if let Some(effect) = &effect {
                    self.record_effect(
                        invocation,
                        result,
                        EffectRecord {
                            status: "planned",
                            identity: effect,
                            capability,
                            caused_by: call_event_id,
                            params: canonical_params,
                            result: None,
                        },
                    )?;
                    self.record_effect(
                        invocation,
                        result,
                        EffectRecord {
                            status: "started",
                            identity: effect,
                            capability,
                            caused_by: call_event_id,
                            params: canonical_params,
                            result: None,
                        },
                    )?;
                }
                let effect_key = effect.as_ref().map(|effect| effect.key.clone());
                let tool_invocation = CapabilityInvocation {
                    capability_id: capability.id.clone(),
                    params: canonical_params.clone(),
                    base_directory: invocation.base_directory.clone(),
                    effect_key: effect_key.clone(),
                };
                let active_abort = RunAbort {
                    external: self.abort,
                    clock: self.clock,
                    deadline_ms: invocation.deadline_ms,
                };
                let tool_result = self
                    .tool_executor
                    .execute(&tool_invocation, &active_abort)
                    .await;
                let tool_result = match tool_result {
                    Ok(tool_result) => validated_tool_result(tool_result, capability),
                    Err(error) => {
                        let (code, message) = match active_abort.reason() {
                            Some(reason) => {
                                ("tool-aborted", format!("tool execution aborted: {reason}"))
                            }
                            None => ("tool-crashed", error.to_string()),
                        };
                        object(json!({
                            "ok": false,
                            "error": {"code": code, "message": message},
                        }))
                    }
                };
                let tool_result = normalize_tool_result(tool_result, parsed.name.as_str());
                let stop = tool_result.get("ok") == Some(&Value::Bool(true))
                    && tool_result.get("stop") == Some(&Value::Bool(true));
                let effect = effect.map(|identity| EffectCompletionContext {
                    identity,
                    capability: capability.clone(),
                    params: canonical_params.clone(),
                });
                Ok(ToolExecution {
                    capability_id: Some(capability.id.clone()),
                    tool_result,
                    stop,
                    effect,
                })
            }
            RouteKind::Wait => {
                let Some(queue_item_id) = &invocation.source_queue_item_id else {
                    return Ok(ToolExecution::control(
                        &route.id,
                        tool_error(
                            "missing-wait-source",
                            "the run does not have a source queue item",
                        ),
                        false,
                    ));
                };
                let handle = control_handle("wait_", queue_item_id, position);
                let event_type = text_param(canonical_params, "event_type");
                let fields = canonical_params
                    .get("fields")
                    .and_then(Value::as_object)
                    .cloned()
                    .unwrap_or_default();
                let deadline = optional_text_param(canonical_params, "deadline");
                let deadline =
                    match normalize_control_time(deadline, "deadline", "invalid-wait-deadline") {
                        Ok(deadline) => deadline,
                        Err(error) => {
                            return Ok(ToolExecution::control(&route.id, error, false));
                        }
                    };
                result.proposals.push(AgentProposal::Wait {
                    handle: handle.clone(),
                    event_type,
                    fields,
                    deadline,
                    position,
                });
                Ok(ToolExecution::control(
                    &route.id,
                    object(json!({"ok": true, "handle": handle, "stop": true})),
                    true,
                ))
            }
            RouteKind::Publish => {
                let Some(queue_item_id) = &invocation.source_queue_item_id else {
                    return Ok(ToolExecution::control(
                        &route.id,
                        tool_error(
                            "missing-publish-source",
                            "the run does not have a source queue item",
                        ),
                        false,
                    ));
                };
                let event_type = text_param(canonical_params, "event_type");
                if !invocation.publishable_events.contains_key(&event_type) {
                    return Ok(ToolExecution::control(
                        &route.id,
                        tool_error(
                            "undeclared-event-type",
                            &format!("the agent does not list event '{event_type}' in publishes"),
                        ),
                        false,
                    ));
                }
                let payload = canonical_params
                    .get("payload")
                    .and_then(Value::as_object)
                    .cloned()
                    .unwrap_or_default();
                let schema = &invocation.publishable_events[&event_type];
                if let Some(schema) = schema.as_object() {
                    if let Some(violation) =
                        schema_violation(&Value::Object(payload.clone()), schema)
                    {
                        return Ok(ToolExecution::control(
                            &route.id,
                            tool_error("invalid-event-payload", &violation.message),
                            false,
                        ));
                    }
                }
                let at = optional_text_param(canonical_params, "at");
                let at = match normalize_control_time(at, "at", "invalid-publish-time") {
                    Ok(at) => at,
                    Err(error) => {
                        return Ok(ToolExecution::control(&route.id, error, false));
                    }
                };
                let handle = control_handle("pub_", queue_item_id, position);
                result.proposals.push(AgentProposal::Publish {
                    handle: handle.clone(),
                    event_type,
                    payload,
                    at,
                    position,
                });
                Ok(ToolExecution::control(
                    &route.id,
                    object(json!({"ok": true, "handle": handle})),
                    false,
                ))
            }
            RouteKind::Cancel => {
                let Some(source_agent_id) = &invocation.source_agent_id else {
                    return Ok(ToolExecution::control(
                        &route.id,
                        tool_error(
                            "missing-cancel-source",
                            "the run does not have an authored agent session",
                        ),
                        false,
                    ));
                };
                let Some(source_session_id) = &invocation.source_session_id else {
                    return Ok(ToolExecution::control(
                        &route.id,
                        tool_error(
                            "missing-cancel-source",
                            "the run does not have an authored agent session",
                        ),
                        false,
                    ));
                };
                let handle = text_param(canonical_params, "handle");
                result.proposals.push(AgentProposal::Cancel {
                    handle: handle.clone(),
                    reason: optional_text_param(canonical_params, "reason"),
                    source_agent_id: source_agent_id.clone(),
                    source_session_id: source_session_id.clone(),
                    position,
                });
                Ok(ToolExecution::control(
                    &route.id,
                    object(json!({"ok": true, "handle": handle, "status": "requested"})),
                    false,
                ))
            }
            RouteKind::QueryContextBudget => Ok(ToolExecution::control(
                &route.id,
                context_budget_result(invocation, result)?,
                false,
            )),
        }
    }

    fn record_effect(
        &mut self,
        invocation: &AgentInvocation,
        run_result: &mut AgentRunResult,
        effect: EffectRecord<'_>,
    ) -> Result<(), AgentRunError> {
        let EffectRecord {
            status,
            identity,
            capability,
            caused_by,
            params,
            result,
        } = effect;
        let mut payload = object(json!({
            "effect_key": identity.key,
            "operation": capability.id,
            "semantics": identity.semantics,
            "scope": identity.scope,
            "queue_item_id": if identity.scope.starts_with("qi_") {
                Some(identity.scope.as_str())
            } else {
                None
            },
            "params": params,
            "status": status,
        }));
        if let Some(result) = result {
            payload.insert("result".to_owned(), Value::Object(result));
        }
        let event_type = format!("runtime.effect.{status}");
        let event_id = format!("{event_type}:{}", identity.key);
        let draft = draft_with_invocation_context(
            invocation,
            event_type.clone(),
            format!("capability:{}", capability.id),
            payload,
            Some(format!("{event_type}:{}", identity.key)),
            Some(caused_by.to_owned()),
        );
        self.record_durable_draft(run_result, &event_id, draft)?;
        Ok(())
    }

    fn record_durable_draft(
        &mut self,
        result: &mut AgentRunResult,
        event_id: &str,
        draft: DraftEvent,
    ) -> Result<String, AgentRunError> {
        let retained_id = self
            .draft_recorder
            .record(event_id, &draft)
            .map_err(AgentRunError::Failed)?;
        result.events.push(draft);
        Ok(retained_id)
    }

    fn record_trace(&mut self, result: &AgentRunResult) -> Result<(), AgentRunError> {
        self.draft_recorder
            .record_trace(&result.trace)
            .map_err(AgentRunError::Failed)
    }

    fn abort_run(
        &mut self,
        invocation: &AgentInvocation,
        mut result: AgentRunResult,
        reason: AbortReason,
        caused_by: Option<String>,
    ) -> Result<AgentRunResult, AgentRunError> {
        result.steps.push(StepName::AbortRun);
        let draft = draft_with_invocation_context(
            invocation,
            "zeta.turn.failed".to_owned(),
            invocation.event_source.clone(),
            object(json!({
                "_timeline_type": "turn_aborted",
                "reason": reason.to_string(),
                "content": format!("(turn aborted: {})", reason.to_string().replace('_', " ")),
            })),
            None,
            caused_by,
        );
        let event_id = format!(
            "zeta.turn.failed:{}",
            invocation.run_id.as_deref().unwrap_or("agent")
        );
        self.record_durable_draft(&mut result, &event_id, draft)?;
        Err(AgentRunError::Aborted(Box::new(AgentRunAborted {
            reason,
            result,
        })))
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

enum RunLoopControl {
    Continue,
    Finish,
    Abort {
        reason: AbortReason,
        caused_by: Option<String>,
    },
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
    model_schema: Map<String, Value>,
    canonical_schema: Map<String, Value>,
    argument_adapter: ArgumentAdapter,
    kind: RouteKind,
}

enum RouteKind {
    External(Box<ResolvedCapability>),
    Publish,
    Wait,
    Cancel,
    QueryContextBudget,
}

impl CapabilitySet {
    fn new(
        invocation: &AgentInvocation,
        declarations: &[ResolvedCapability],
    ) -> Result<Self, AgentError> {
        let mut by_id = HashMap::new();
        let mut declared_names = HashMap::new();
        let mut declared_ids = HashSet::new();
        for declaration in declarations {
            if by_id
                .insert(declaration.canonical.id.as_str(), declaration)
                .is_some()
            {
                return Err(AgentError::invocation(format!(
                    "duplicate capability id: {}",
                    declaration.canonical.id
                )));
            }
            let name = declaration.model_name.clone();
            let count = declared_names.entry(name).or_insert(0);
            *count += 1;
            declared_ids.insert(declaration.canonical.id.to_string());
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
            let kind = match declaration.canonical.id.as_str() {
                "zeta.query_content"
                | "zeta.transform_content"
                | "zeta.finish"
                | "zeta.query_log" => {
                    return Err(AgentError::invocation(format!(
                        "unsupported capability grant: {id}"
                    )));
                }
                "zeta.query_context_budget" => RouteKind::QueryContextBudget,
                _ => RouteKind::External(Box::new((*declaration).clone())),
            };
            set.add_route(
                declaration.model_name.as_str(),
                Route {
                    id: declaration.canonical.id.clone(),
                    model_schema: declaration.model_input_schema.clone(),
                    canonical_schema: declaration.canonical.input_schema.clone(),
                    argument_adapter: declaration.argument_adapter.clone(),
                    kind,
                },
                declaration.model_description.as_str(),
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
                "Cancel an active wait or pending deferred publication from this session.",
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
        self.add_route(
            name,
            Route {
                id,
                model_schema: schema.clone(),
                canonical_schema: schema,
                argument_adapter: ArgumentAdapter::Identity,
                kind,
            },
            description,
        )
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
                    "parameters": route.model_schema,
                },
        }));
        self.routes.insert(name.to_owned(), route);
        Ok(())
    }

    fn validate<'a>(
        &'a self,
        parsed: &ParsedToolCall,
    ) -> Result<ValidatedRoute<'a>, (String, String)> {
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
        if let Some(message) =
            schema_error(&Value::Object(parsed.params.clone()), &route.model_schema)
        {
            return Err((
                "invalid-tool-args".to_owned(),
                format!("model arguments: {message}"),
            ));
        }
        let canonical_params = match route.argument_adapter.adapt(&parsed.params) {
            Ok(params) => params,
            Err(error) => {
                return Err((
                    "invalid-tool-args".to_owned(),
                    format!("could not adapt model arguments: {error}"),
                ));
            }
        };
        if let Some(message) = schema_error(
            &Value::Object(canonical_params.clone()),
            &route.canonical_schema,
        ) {
            return Err((
                "invalid-tool-args".to_owned(),
                format!("canonical arguments: {message}"),
            ));
        }
        Ok(ValidatedRoute {
            route,
            canonical_params,
        })
    }
}

struct ValidatedRoute<'a> {
    route: &'a Route,
    canonical_params: Map<String, Value>,
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
                parse_error: Some(stable_json_error(arguments, &error)),
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

struct ToolExecution {
    capability_id: Option<CapabilityId>,
    tool_result: Map<String, Value>,
    stop: bool,
    effect: Option<EffectCompletionContext>,
}

fn context_budget_result(
    invocation: &AgentInvocation,
    result: &AgentRunResult,
) -> Result<Map<String, Value>, AgentError> {
    let prompt_tokens = provider_prompt_tokens(&result.telemetry)
        .map(|tokens| (tokens, "provider"))
        .or_else(|| estimated_latest_prompt_tokens(result).map(|tokens| (tokens, "estimate")));
    let context_window_tokens = result
        .telemetry
        .get("model_context_tokens")
        .and_then(Value::as_u64)
        .filter(|tokens| *tokens > 0);
    let reserved_output_tokens = invocation.max_tokens;
    let remaining_tokens = match (context_window_tokens, prompt_tokens) {
        (Some(context), Some((prompt, _source))) => {
            Some(i128::from(context) - i128::from(prompt) - i128::from(reserved_output_tokens))
        }
        (None, _) | (_, None) => None,
    };
    let usage_ratio = match (context_window_tokens, prompt_tokens) {
        (Some(context), Some((prompt, _source))) if context > reserved_output_tokens => {
            Some(prompt as f64 / (context - reserved_output_tokens) as f64)
        }
        (Some(_), Some((_, _))) | (None, _) | (_, None) => None,
    };
    let (compaction_strategy, default_threshold) = match &invocation.prompt_transform {
        PromptTransform::None => ("off", None),
        PromptTransform::StructuralTrim { .. } => ("structural_trim", None),
        PromptTransform::DropOldest { max_tokens } => ("drop_oldest", Some(*max_tokens)),
    };
    let compaction_threshold_tokens = invocation.compaction_threshold_tokens.or(default_threshold);
    Ok(object(json!({
        "ok": true,
        "context_window_tokens": context_window_tokens,
        "prompt_tokens": prompt_tokens.map(|(tokens, _source)| tokens),
        "prompt_tokens_source": prompt_tokens
            .map(|(_tokens, source)| source)
            .unwrap_or("unavailable"),
        "reserved_output_tokens": reserved_output_tokens,
        "remaining_tokens": remaining_tokens,
        "usage_ratio": usage_ratio,
        "compaction_strategy": compaction_strategy,
        "compaction_threshold_tokens": compaction_threshold_tokens,
    })))
}

fn provider_prompt_tokens(telemetry: &Map<String, Value>) -> Option<u64> {
    let values = telemetry
        .get("usage")
        .and_then(Value::as_object)
        .unwrap_or(telemetry);
    values
        .get("prompt_tokens")
        .or_else(|| values.get("input_tokens"))
        .and_then(Value::as_u64)
}

fn estimated_latest_prompt_tokens(result: &AgentRunResult) -> Option<u64> {
    let prompt_id = &result.prompt_traces.last()?.prompt_object_id;
    let prompt = result
        .trace
        .objects
        .iter()
        .find(|row| row.id == *prompt_id)?;
    let mut tokens = 0_u64;
    for link in &prompt.object.links {
        let Some(component) = result.trace.objects.iter().find(|row| row.id == *link) else {
            continue;
        };
        let bytes = canonical_json(&Value::Object(component.object.data.clone())).ok()?;
        let text = String::from_utf8(bytes).ok()?;
        if !text.is_empty() {
            tokens += text.chars().count().div_ceil(4).max(1) as u64;
        }
    }
    Some(tokens)
}

impl ToolExecution {
    fn control(capability_id: &CapabilityId, tool_result: Map<String, Value>, stop: bool) -> Self {
        ToolExecution {
            capability_id: Some(capability_id.clone()),
            tool_result,
            stop,
            effect: None,
        }
    }
}

struct EffectCompletionContext {
    identity: EffectIdentity,
    capability: Capability,
    params: Map<String, Value>,
}

struct EffectIdentity {
    key: String,
    scope: String,
    semantics: DeliverySemantics,
}

struct EffectRecord<'a> {
    status: &'a str,
    identity: &'a EffectIdentity,
    capability: &'a Capability,
    caused_by: &'a str,
    params: &'a Map<String, Value>,
    result: Option<Map<String, Value>>,
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
    invocation: &AgentInvocation,
    payload: Map<String, Value>,
    event_id: &str,
    caused_by: Option<String>,
) -> DraftEvent {
    draft_with_invocation_context(
        invocation,
        "zeta.model_call.completed".to_owned(),
        invocation.event_source.clone(),
        payload,
        Some(format!("zeta.model_call.completed:{event_id}")),
        caused_by,
    )
}

fn tool_draft(
    invocation: &AgentInvocation,
    event_type: &str,
    payload: Map<String, Value>,
    event_id: &str,
    caused_by: Option<&str>,
) -> DraftEvent {
    draft_with_invocation_context(
        invocation,
        event_type.to_owned(),
        invocation.event_source.clone(),
        payload,
        Some(format!("{event_type}:{event_id}")),
        caused_by.map(str::to_owned),
    )
}

fn draft_with_invocation_context(
    invocation: &AgentInvocation,
    event_type: String,
    source: String,
    payload: Map<String, Value>,
    idempotency_key: Option<String>,
    caused_by: Option<String>,
) -> DraftEvent {
    DraftEvent {
        event_type,
        source,
        payload,
        idempotency_key,
        caused_by,
        session_id: invocation.session_id.clone(),
        run_id: invocation.run_id.clone(),
        turn_id: invocation.turn_id.clone(),
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

fn tool_call_id(value: &Value, index: usize) -> String {
    let id = value.get("id").and_then(Value::as_str).unwrap_or("");
    if id.is_empty() {
        format!("call-{index}")
    } else {
        id.to_owned()
    }
}

fn terminal_tool_result<'a>(drafts: &'a [DraftEvent], call_id: &str) -> Option<&'a DraftEvent> {
    for draft in drafts.iter().rev() {
        if draft.payload.get("_timeline_type").and_then(Value::as_str) != Some("tool_result") {
            continue;
        }
        if draft.payload.get("tool_call_id").and_then(Value::as_str) != Some(call_id) {
            continue;
        }
        let status = draft
            .payload
            .get("status")
            .and_then(Value::as_str)
            .unwrap_or("");
        if status == "completed"
            || status == "failed"
            || status == "refused"
            || status == "cancelled"
            || status == "timed_out"
        {
            return Some(draft);
        }
    }
    None
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
        let message = match &error.kind {
            ValidationErrorKind::Required { property } => {
                let property = property.as_str().unwrap_or("");
                format!("'{property}' is a required property")
            }
            _ => error.to_string(),
        };
        let violation = SchemaViolation { location, message };
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

fn stable_json_error(input: &str, error: &serde_json::Error) -> String {
    if error.classify() == serde_json::error::Category::Eof {
        let character = input.chars().count();
        let column = character + 1;
        return format!(
            "Expecting value: line {} column {column} (char {character})",
            error.line(),
        );
    }
    error.to_string()
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
    params: &Map<String, Value>,
) -> Result<Option<EffectIdentity>, AgentError> {
    let Some(semantics) = capability.delivery_semantics else {
        return Ok(None);
    };
    let scope = invocation
        .effect_scope
        .as_deref()
        .or(invocation.source_queue_item_id.as_deref())
        .unwrap_or(parsed.call_id.as_str());
    let value = json!({
        "scope": scope,
        "operation": capability.id.as_str(),
        "params": params,
    });
    let bytes = canonical_json(&value).map_err(|error| AgentError::identity(error.to_string()))?;
    Ok(Some(EffectIdentity {
        key: format!("effect:{}", derive(Domain::Chain, &bytes)),
        scope: scope.to_owned(),
        semantics,
    }))
}

fn effect_terminal_status(
    semantics: DeliverySemantics,
    result: &Map<String, Value>,
) -> &'static str {
    if result.get("ok") == Some(&Value::Bool(true)) {
        return "completed";
    }
    if semantics == DeliverySemantics::UnsafeToRetry {
        "ambiguous"
    } else {
        "failed"
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

fn normalize_control_time(
    value: Option<String>,
    field: &str,
    code: &str,
) -> Result<Option<String>, Map<String, Value>> {
    let Some(value) = value else {
        return Ok(None);
    };
    if !has_utc_offset(&value) {
        if looks_like_offset_free_datetime(&value) {
            return Err(tool_error(
                code,
                &format!("{field} must include a UTC offset"),
            ));
        }
        return Err(tool_error(
            code,
            &format!("{field} must be an ISO 8601 date-time with a UTC offset"),
        ));
    }
    let parsed = match OffsetDateTime::parse(&value, &Rfc3339) {
        Ok(parsed) => parsed,
        Err(_error) => {
            return Err(tool_error(
                code,
                &format!("{field} must be an ISO 8601 date-time with a UTC offset"),
            ));
        }
    };
    let utc = parsed.to_offset(UtcOffset::UTC);
    let formatted = utc
        .format(&Rfc3339)
        .map_err(|error| tool_error(code, &error.to_string()))?;
    let formatted = match formatted.strip_suffix('Z') {
        Some(prefix) => format!("{prefix}+00:00"),
        None => formatted,
    };
    Ok(Some(formatted))
}

fn has_utc_offset(value: &str) -> bool {
    if value.ends_with('Z') || value.ends_with('z') {
        return true;
    }
    let Some(time_index) = value.find('T').or_else(|| value.find('t')) else {
        return false;
    };
    let time = &value[time_index + 1..];
    for character in time.chars() {
        if character == '+' || character == '-' {
            return true;
        }
    }
    false
}

fn looks_like_offset_free_datetime(value: &str) -> bool {
    if value.len() < 19 {
        return false;
    }
    let bytes = value.as_bytes();
    bytes.get(4) == Some(&b'-')
        && bytes.get(7) == Some(&b'-')
        && (bytes.get(10) == Some(&b'T') || bytes.get(10) == Some(&b't'))
        && bytes.get(13) == Some(&b':')
        && bytes.get(16) == Some(&b':')
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
