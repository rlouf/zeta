# Voice architecture for Zeta

Status: design note — 2026-07-28. This is an analysis of the checked-in
implementation and current official OpenAI documentation, not a commitment to
an OpenAI-only product.

## 1. Executive summary

**Recommendation: use a chained voice pipeline. Zeta owns the semantic turn,
tool selection, tool execution, and durable history. A browser/mobile client
owns microphone capture and immediate playback stop. OpenAI Realtime is used
only as an optional low-latency *transcription* service (`gpt-live-transcribe`),
and OpenAI's Audio Speech endpoint is used to speak Zeta's exact text. Do not
put a `gpt-realtime-*` conversation model in the v0 semantic path.**

That is Architecture B with one specific implementation choice: use a Realtime
**transcription** session for streaming ASR and endpointing, not a Realtime
**voice-agent** session. It preserves a single semantic authority and lets a
voice turn enter the same Zeta session used by Commas/CLI. The configured Zeta
model can remain a local OpenAI-compatible endpoint, Codex Responses backend,
or a future provider.

```text
browser / mobile                         Zeta voice gateway
----------------                         ------------------
mic ──WebRTC──> OpenAI transcription ──final transcript──> durable Zeta user turn
                    (`gpt-live-transcribe`)                  │
             <── partial transcript / VAD ────────────────────┤
                                                               v
                                                    existing Zeta agent loop
                                                    configured model + tools
                                                               │ text deltas
                                                               v
                                                        sentence segmenter
                                                               │
speaker <── streamed PCM/WAV <── Audio Speech API <────────────┘
                         (`gpt-4o-mini-tts`, server-authenticated)
```

This deliberately gives up native speech-to-speech's best traits: one model can
use vocal affect directly, begin an answer before a finished transcript, and
maintain the audiovisual conversation itself. It adds an ASR boundary and a
TTS boundary, so first audible answer latency is approximately:

```text
endpointing + final ASR + Zeta time-to-first-text + segment boundary + TTS time-to-first-audio
```

There is no fixed OpenAI latency promise for those terms; they must be measured
with Zeta's actual configured models and microphones. The benefit is more
important for Zeta: one durable event log, one tool policy, one confirmation
path, one source of truth for clarification questions, and continuity with CLI
sessions. OpenAI itself describes the chained architecture as the fit for
extending an existing text agent and for workflows needing explicit control of
the intermediate text. [Voice agents](https://developers.openai.com/api/docs/guides/voice-agents)

## 2. Current Zeta interaction loop

### What a conversation is

Zeta does not store a mutable `Conversation` object. A conversation is the
model-visible projection of append-only events in one `RuntimeContext.session_id`.
`zeta.run.runtime.current_timeline()` reads the session's `zeta.*` events and
keeps only `user_message`, `model`, `model_usage`, `tool_call`, `tool_result`,
and `turn_aborted` ([runtime.py](../zeta/src/zeta/run/runtime.py)). The durable
event journal is SQLite; prompt/object provenance is a separate SQLite-backed
object store created by `session_for_id()` ([context.py](../zeta/src/zeta/run/context.py)).

This is a strong voice seam: a voice transcript can be a normal durable user
message in an existing session. It does **not** require a separate voice
conversation store.

There are two closely related entry paths:

- Commas is the interactive CLI conversation frontend. Its session is
  `COMMAS_SESSION_ID` (default `default`) and `zeta_session_for_commas()` builds
  the Zeta `RuntimeContext` in the shared state directory
  ([commas `__init__.py`](../commas/src/commas/__init__.py),
  [sessions.py](../commas/src/commas/sessions.py)).
- The daemon/RPC path uses `session.turn.requested` to start the same native
  run loop. `session.run` creates a run id and an `asyncio.Event` cancellation
  token ([routes.py](../zeta/src/zetad/rpc/routes.py),
  [thread_run.py](../zeta/src/zeta/run/thread_run.py)).

### Complete CLI trace: `commas ask "what changed?"`

```text
commas.cli.step.cmd_ask
  -> commas.workflows.ask.ask
  -> commas.workflows.step.step
      -> zeta_session_for_commas()              # selects the durable session
      -> active_model_selection(session_dir=…)   # selects a provider/profile
      -> record_user_message()                  # zeta.user_message
      -> append_prompt_submitted_event()         # zeta.prompt.submitted
      -> current_timeline()                      # prior durable events
      -> run_agent_loop(...)
          -> PromptBuilder.plan/commit/render
          -> DefaultModelGateway.generate
          -> record model event / execute tools / repeat
      -> TurnRecorder.finish()                   # zeta.turn.completed/failed
      -> render_final_answer()                   # stdout
```

Concretely, `ask()` adds CLI-specific recent shell context then calls `step()`
([ask.py](../commas/src/commas/workflows/ask.py),
[step.py](../commas/src/commas/workflows/step.py)). `step()` records the user
payload (prompt, workflow, system prompt, available tools, model metadata, and
turn id) as `zeta.user_message`; `append_prompt_submitted_event()` creates the
causally linked `zeta.prompt.submitted` audit record
([state.py](../commas/src/commas/state.py)). Both are in the same SQLite event
journal.

`run_agent_loop()` initializes `AgentRun` and repeats `step()` until there is a
final model answer, a staged effect, abort, or `max_turns`
([runtime.py](../zeta/src/zeta/run/runtime.py)). `build_prompt_step()` sends:

1. the durable timeline from earlier CLI/voice turns;
2. events accumulated during the currently open run; and
3. system prompt, current objective, tool descriptors, project context, and
   compaction policy.

`PromptBuilder.commit_prompt_plan()` stores the exact components and request
hash before the provider is called ([builder.py](../zeta/src/zeta/context/builder.py)).
That is why prompt replay works even though the working conversation is an
event projection.

When the model returns tool calls, `step_model()` records one `model` event,
puts the calls in `RunState.pending_tool_calls`, and `step_tools()` processes
them. `handle_tool_call()` validates JSON, allow-list membership, and JSON
Schema; emits a `tool_call` event; invokes the registered capability; then
emits a terminal `tool_result` event
([runtime.py](../zeta/src/zeta/run/runtime.py),
[execution.py](../zeta/src/zeta/capabilities/execution.py)). The next model
call receives those result events as current-run context. A run can therefore
remain open across arbitrarily alternating model/tool steps, bounded by
`max_turns` (25 by default), rather than equating one user turn to one model
request.

For direct side effects, capability execution also journals
`runtime.effect.planned`, `started`, then `completed`, `failed`, or `ambiguous`
with an idempotency/effect key. This is the durable boundary described in
[runtime semantics](runtime-semantics.md). It is valuable for voice: an
interrupted sentence must never erase evidence that an action was already
started.

### Provider selection and streaming

`active_model_selection()` resolves a session-selected profile, then the
default from `~/.zeta/models.toml`, then the built-in Codex profile. A profile uses either
`chat-completions` or `codex-responses`; `DefaultModelGateway` normalizes both
to Zeta's `ModelOutput` ([profiles.py](../zeta/src/zeta/models/profiles.py),
[models `__init__.py`](../zeta/src/zeta/models/__init__.py)). This is genuine
model-loop independence, not merely a model-name setting.

Zeta already streams model text. Both adapters feed `content_delta()` and
`reasoning_delta()` into `ModelTurnStreamSink`, producing
`runtime.stream.chunk` and `runtime.status.update`
([streaming.py](../zeta/src/zeta/run/streaming.py),
[chat_completions.py](../zeta/src/zeta/models/chat_completions.py),
[responses.py](../zeta/src/zeta/models/responses.py)). Commas renders content
immediately through `TraceAwareStreamRenderer`.

Those stream chunks are deliberately **UI events**, not durable timeline facts:
`is_runtime_ui_event()` excludes them from prompt projection and the RPC
`run_agent()` sink drops them. The completed normalized assistant/model event
is durable. A voice client needs a new transient-progress subscription; it
must not make every token a durable message simply to play speech.

### Cancellation, text coupling, and reusable seams

Cancellation exists but is not yet realtime-grade:

- `session.cancel` sets the per-RPC-run `asyncio.Event`.
- `check_run_abort()` examines that token before a model step, before a tool
  step, and after a model request, then records a `turn_aborted` event.
- The Chat Completions request runs as a blocking streaming call in
  `asyncio.to_thread`; capability invocation can also run in a thread.
  Setting the token does **not** close an in-flight provider HTTP stream or
  stop an in-flight tool. It prevents later steps after control returns.
- Commas catches `KeyboardInterrupt` and records a failed/aborted turn, but
  has no audio-aware interruption path.

The text-coupled pieces are `ModelInput.messages`, provider chat/Responses
adapters, `AssistantMessage.content`, terminal renderers, prompt components,
and user-message payloads named `content`. The naturally reusable pieces are
`RuntimeContext`, session ids, event/provenance stores, `AgentRun`'s
model/tool state machine, `ModelStream` callbacks, capability registry,
effect semantics, and `CancellationToken`.

## 3. Relevant OpenAI API capabilities

The table separates documented/supported behavior from tempting but unsupported
inferences. Links are the official source for each material claim.

| Capability | What the current API explicitly supports | Design consequence |
| --- | --- | --- |
| Realtime voice-agent session | A stateful `/v1/realtime` conversation session accepts audio or text; the realtime model creates responses, calls tools, and manages the session conversation. | It is a full second agent loop, not just an audio transport. [Realtime and audio](https://developers.openai.com/api/docs/guides/realtime) |
| Realtime transcription-only session | `type: "transcription"` with `gpt-live-transcribe` emits incremental transcript deltas and a final completion event, with no model-generated spoken response. Audio may be explicitly committed or VAD-chunked. | This is the clean realtime component for Zeta-owned semantics. [Realtime transcription](https://developers.openai.com/api/docs/guides/realtime-transcription) |
| Realtime text | A client can create a text user item and call `response.create`; text output streams as `response.output_text.delta`. Output modality can be text-only. | Text acceptance is model input, not evidence of a neutral TTS API. [Realtime conversations](https://developers.openai.com/api/docs/guides/realtime-conversations) |
| Audio configuration | Realtime config contains `session.audio.input` format/transcription/turn detection and `audio.output` format/voice; output modality may be audio or text. | Useful if Zeta later elects to use a native Realtime agent, not required for v0 ASR+TTS. [Realtime conversations](https://developers.openai.com/api/docs/guides/realtime-conversations) |
| VAD / endpointing | `server_vad` uses silence and offers threshold/prefix/silence controls; `semantic_vad` uses a semantic classifier and supports eagerness. Both emit speech-started/stopped. In transcription sessions they chunk audio rather than create replies. | Client reacts to `speech_started` immediately; start with semantic VAD `auto`/`medium`, benchmark it, and retain push-to-talk fallback. [Realtime VAD](https://developers.openai.com/api/docs/guides/realtime-vad) |
| Disable autonomous responses | In a Realtime conversation session, `turn_detection.create_response=false` and `interrupt_response=false` retain VAD but require the app to call `response.create`. | This prevents automatic reply creation, but manually calling `response.create` still asks the Realtime model to formulate a response. [Keep VAD, disable automatic responses](https://developers.openai.com/api/docs/guides/realtime-conversations#keep-vad-but-disable-automatic-responses) |
| Realtime interruptions | With VAD, user speech cancels an active response. WebRTC/SIP automatically knows played audio and truncates it; WebSocket clients must stop playback, observe `response.cancelled`, and send `conversation.item.truncate` with the played audio offset. | This is excellent behavior **inside a Realtime conversation**, but does not cancel Zeta or a separate Speech request. [Interruption and truncation](https://developers.openai.com/api/docs/guides/realtime-conversations#interruption-and-truncation) |
| Realtime function calls | Session/response tools are JSON-Schema functions. Argument deltas arrive in `response.function_call_arguments.delta`; full calls arrive by `response.done`. The client returns `function_call_output` correlated by `call_id`, then calls `response.create` again. | A Realtime model can own tools, but doing so transfers tool selection to it. Never execute an irreversible tool from an argument delta; wait for completion and validate. [Realtime conversations: Function calling](https://developers.openai.com/api/docs/guides/realtime-conversations#function-calling) |
| Independent TTS | `POST /v1/audio/speech` accepts exact input text, voice, and style instructions. It streams audio with chunked transfer; PCM or WAV are recommended for fastest playback. | This is the supported exact-text speech renderer for Zeta's answer. [Text to speech](https://developers.openai.com/api/docs/guides/text-to-speech) |
| Request/file STT | The transcription API supports streamed transcript events for an already completed recording or application-controlled turn. | Good push-to-talk/fallback ASR; it is not the lowest-latency live-session path. [Speech to text](https://developers.openai.com/api/docs/guides/speech-to-text) |
| Responses API | `stream=true` uses typed SSE events including `response.output_text.delta`, and function-argument delta/done events. Tool output is a `function_call_output` item matched by `call_id`. | Zeta's Codex Responses adapter already maps text deltas and final tool calls. It is not a realtime microphone/audio-session API. [Streaming Responses](https://developers.openai.com/api/docs/guides/streaming-responses), [Function calling](https://developers.openai.com/api/docs/guides/function-calling) |
| Agents SDK | TypeScript offers `RealtimeAgent`/`RealtimeSession`; Python offers a chained `VoicePipeline`. The official guide calls the former best for native low-latency speech-to-speech and the latter a fit for existing text-agent control. | It is an optional client implementation aid, not a replacement for Zeta's runtime. [Voice agents](https://developers.openai.com/api/docs/guides/voice-agents) |
| Browser/mobile topology | OpenAI recommends WebRTC over WebSockets for browser/mobile realtime media. A trusted server either creates the WebRTC call or mints a short-lived client secret; standard API keys must not reach the browser. | Browser talks to OpenAI for transcription media; Zeta owns a separate authenticated control channel. [Realtime WebRTC](https://developers.openai.com/api/docs/guides/realtime-webrtc) |

### Unsupported or ambiguous capabilities

1. **A neutral Realtime TTS operation is not documented.** The documented
   Realtime audio output is the output of a Realtime `response.create` model
   response. The documented exact-text renderer is the independent Audio Speech
   endpoint. Adding Zeta's text as a conversation item and asking Realtime for
   audio would add another generative model turn; it may paraphrase, acknowledge,
   or otherwise exercise semantic control. The API accepting text input does
   not change that. [Realtime conversations](https://developers.openai.com/api/docs/guides/realtime-conversations), [Text to speech](https://developers.openai.com/api/docs/guides/text-to-speech)
2. It is technically possible to disable automatic realtime responses and
   manually create one, but it is not a documented way to turn arbitrary
   external text into identical speech. Treat any prompt-based workaround as an
   experiment, not an architecture contract.
3. Realtime has an ephemeral conversation capped at 60 minutes; it is not a
   replacement for Zeta's durable history. Its transcript/event ordering also
   has caveats: final transcription completion ordering across turns is not
   guaranteed, so `item_id` must correlate them. [Realtime conversations](https://developers.openai.com/api/docs/guides/realtime-conversations), [Realtime transcription](https://developers.openai.com/api/docs/guides/realtime-transcription)
4. OpenAI documents control flows, not an end-to-end latency SLO for the
   composition above. Do not put numeric latency promises in product planning
   before measuring endpointing, configured Zeta model time-to-first-text, TTS,
   and device output under representative conditions.

## 4. Architecture alternatives

| Architecture | Semantic authority / tools | Benefits | Cost / verdict |
| --- | --- | --- | --- |
| A. Realtime owns loop | Realtime selects calls and answers; Zeta is direct tools or a delegated backend. | Best native interruption, conversational pacing, and potential first-audio latency. | Sacrifices configured-model independence and duplicates Zeta's event/history reasoning. **Not recommended for Zeta's primary conversation.** |
| B. Zeta + streaming STT/TTS | Zeta selects and executes all domain tools. | One durable semantic history; exact answer speech; reuse CLI and every configured model. | Extra ASR/TTS latency and less native prosody. **Recommended.** |
| C. Realtime audio/session, Zeta semantic authority | Desired: Realtime only handles media, speaks arbitrary Zeta text. | Would combine native audio with Zeta authority. | The clean neutral-speech primitive is not documented. A Realtime `response.create` necessarily reintroduces a model answer. **Reject as stated.** |
| D. Controlled two-agent split | Realtime handles acknowledgements/pacing, Zeta decisions/actions. | May feel lively for a narrowly scripted concierge. | Two writers to the conversation and two tool-decision loci. **Do not use for durable Zeta conversations.** |

### A. OpenAI Realtime owns the live conversational loop

Realtime has the needed native mechanics: speech-to-speech, VAD, interruption,
function calls, and function-result continuation. If it receives every Zeta
tool directly, its model chooses the tool and interprets its result. Zeta then
is an audited tool host, not the agent loop. Durable Zeta events can record the
calls, but the Realtime conversation becomes a second, ephemeral source of
semantic state. CLI and voice must be manually synchronized both directions.

Giving Realtime one `delegate_to_zeta` tool changes the shape, not the problem.
Zeta can return an authoritative text answer, but Realtime must then generate
the spoken response after receiving the tool output. That extra model boundary
can summarize, soften, contradict, ask a different clarification, or issue a
second call. A prompt saying "repeat it verbatim" is not a transactional
guarantee. It also adds a model round trip after Zeta completes.

This is a rational future product option only if Zeta intentionally chooses
OpenAI Realtime as its primary model and accepts that voice conversations are a
separate, OpenAI-owned agent experience. It is not the clean extension of the
current repository.

#### Realtime is a live model session, not a text-agent trigger

If Zeta deliberately offers this as a separate, OpenAI-coupled voice mode, it
must still project the durable semantic outcome into the normal Zeta timeline:

```text
Realtime server event -> RealtimeLiveModelSession -> canonical Zeta journal
                                                   -> Zeta capability executor
```

Persist a final `zeta.user_message` (with `source=voice.realtime`), the final
Realtime assistant transcript as `zeta.model`, and the existing tool, result,
and effect events. Persist `voice.assistant.audible` and interruption records
when needed to describe what was actually heard. Audio frames, transcript
deltas, raw Realtime protocol events, and unplayed output are transport state,
not conversation history.

Critically, the current daemon's text-model run is started by the
`session.turn.requested` event, not by a `zeta.user_message` timeline event.
The Realtime live session must therefore **not** call `session.run`, publish
`session.turn.requested`, or invoke `run_agent_loop()`. It directly returns
Zeta capability results as Realtime `function_call_output` items and lets the
Realtime model continue. This preserves CLI continuity without accidentally
starting a second (for example, Codex) run.

This needs one provider-native `RealtimeLiveModelSession` service/module, not
a second semantic agent and not one agent file per voice conversation. It
implements the Realtime provider's live-session contract: projects final
user/assistant messages, exchanges tool calls and results, and propagates
interruption in both directions. It reuses Zeta's capability executor and event
sink, but keeps the live bidirectional Realtime state machine outside the
request/response-oriented text run path. A CLI turn later consumes the
projected timeline through the normal selected provider; no Realtime session
state is assumed to survive its 60-minute limit.
[Realtime conversations](https://developers.openai.com/api/docs/guides/realtime-conversations)

#### Model gateways versus live model sessions

Zeta's ordinary model integrations are turn gateways: Zeta renders a timeline
into one request, receives one model output/stream, and persists the outcome.
The provider does not own a live conversation that Zeta must continuously
synchronize. This is the shape of today's `DefaultModelGateway.generate()`.

A Realtime session is a different provider capability: a long-lived
bidirectional protocol whose input audio, VAD, output audio, function-call
continuations, and interruption race with one another. It needs a live-session
interface rather than an overloaded request/response gateway:

```python
class ModelProvider:
    async def generate(self, input: ModelInput) -> ModelOutput: ...
    async def open_live_session(self, config: LiveSessionConfig) -> LiveModelSession: ...

class LiveModelSession:
    async def receive_audio(self, audio: AudioChunk) -> None: ...
    async def receive_tool_result(self, result: ToolResult) -> None: ...
    async def interrupt(self, reason: str) -> None: ...
    def events(self) -> AsyncIterator[LiveModelEvent]: ...
```

Chat Completions, Responses, and Codex need only `generate()`. A
`gpt-realtime-*` provider implements `open_live_session()` as
`RealtimeLiveModelSession`. That session owns the provider protocol state,
Realtime response/call IDs, and cancellation boundary.

It becomes a design smell only if it reimplements prompt/history construction,
tool validation or execution, effects and retries, durable event semantics, or
a second planner. Those remain Zeta responsibilities. A live model session is
a provider integration, not another agent.

| Integration shape | Examples | Dedicated lifecycle component? |
| --- | --- | --- |
| Turn ingress | CLI, HTTP chat, a Slack message | Normally no: append a message, publish `session.turn.requested`, and render the result. |
| Model turn gateway | Chat Completions, Responses, Codex | Existing Zeta shape: normalize one model request/stream. |
| Live model session | OpenAI Realtime; a future equivalent | Yes: a provider-native live session owns the bidirectional protocol and native interruption/function events. |
| Voice channel | Browser microphone/speaker, SIP/phone | Sometimes: owns media/call lifecycle and delegates semantic work to a turn path or live model session. |
| Collaborative surface | Shared canvas, IDE co-pilot, screen/control session | Often: continuous shared state and interruptions do not fit one request. |
| Background execution | Queue workers, schedules, webhooks | Already represented by Zeta's worker/coordinator machinery. |
| Human approval | UI confirmation, Slack button, spoken confirmation | Usually small: records a decision that releases or rejects a staged effect. |

Introduce the `LiveModelSession` abstraction only when a second genuine
live-session provider needs it. Until then, `RealtimeLiveModelSession` can be a
provider-specific implementation that shares only Zeta's event and capability
contracts.

### B. Zeta owns semantics; streaming STT plus streaming TTS

Use a dedicated Realtime transcription session with `gpt-live-transcribe` for
live audio, semantic/server VAD, partial UI transcript, and final committed
transcript. On the final transcript only, call the existing `run_agent_loop()`
in the selected Zeta session. Feed its text deltas into a conservative sentence
or clause segmenter and issue Audio Speech requests for completed segments.
The Speech API starts sending audio before it has generated the complete audio
for that segment; it does not require the full Zeta answer. [Text to speech](https://developers.openai.com/api/docs/guides/text-to-speech)

This is practical, with two qualifications:

- Segment only on stable punctuation (and a maximum-wait fallback), never on
  each token. That avoids speaking words the model may immediately qualify and
  gives TTS enough text for prosody. Queue segments in order and keep a small
  playback buffer.
- It will sound less jointly expressive than native speech-to-speech. The TTS
  model can take style instructions and choose a voice, but it does not hear
  the user's tone or participate in the semantic turn. Zeta should therefore
  generate voice-friendly, concise text rather than rely on a second model to
  rewrite it.

Clarifications are ordinary Zeta answers: the authoritative model asks the
question, it is spoken, and the next final transcript is the next durable user
turn. No voice-specific semantic agent is needed.

### C. Realtime as audio/session layer and Zeta as semantic authority

There is a narrow supported version of this: Realtime **transcription** is an
audio session layer with no model-generated answer. That is the recommended
architecture above.

The stronger version — one Realtime conversation that accepts microphone audio,
never formulates answers, and speaks exact Zeta-provided text with native
Realtime quality — is not currently established by the docs. Realtime lets the
app disable automatic responses, but its documented way to obtain audio is to
ask the realtime model to create a response. There is no documented
``speak(input_text)`` event. Keeping both Zeta's event log and a Realtime
conversation synchronized would also require mirroring every user transcript,
tool result, final assistant answer, and interruption truncation.

Do not build this around an inference from `conversation.item.create`. The
smallest admissible spike is listed below; until it shows byte-for-byte
verbatim, model-free speaking with correct interruption semantics, C is not a
viable foundation.

### D. Controlled two-agent split

The only stable-looking boundary is extremely small: Realtime may provide
non-semantic acoustic cues such as a local earcon or a static "one moment"
while Zeta works. It may not promise an action, paraphrase a result, ask a
clarification, select a domain tool, or write a durable assistant turn.

Even that acknowledgement should be presented as local UI, not model truth.
Otherwise failure modes are structural: it can confirm before Zeta's tool
completes, ask a question Zeta would not ask, call an overlapping tool, or leave
two incompatible histories after an interruption. For durable work, no second
agent should speak on Zeta's behalf.

## 5. Barge-in semantics

"Barge-in" is eight separate contracts. The recommended behavior is below.

| Concern | Required behavior and durable meaning |
| --- | --- |
| 1. Stop device playback | On `speech_started` or press-to-talk, the client stops its audio node immediately. This is local and must not wait for Zeta or network acknowledgment. |
| 2. Detect user speech | Browser observes transcription-session `input_audio_buffer.speech_started`; semantic/server VAD endpoints the next transcript. Push-to-talk is a supported fallback when VAD is wrong. |
| 3. Stop server TTS | Abort the current Speech HTTP stream and discard queued, unplayed TTS segments. The browser sends `voice.playback.interrupted` with current segment/played duration. |
| 4. Cancel Zeta generation | Send the active `run_id` to a server-owned run registry. Cancellation must close/cancel the provider stream, not merely set today's cooperative token. No later model call or new tool may start after the cancellation barrier. |
| 5. Partial assistant text in history | Do not write the full uncompleted generated text as an assistant result. Persist an explicit aborted turn and the conservative text actually delivered to the speaker. |
| 6. Heard versus generated text | Audio Speech has no word-alignment contract. Persist only whole, fully played TTS segments as `voice.assistant.audible`; on a mid-segment cut, omit the unfinished sentence. The next prompt receives an explicit partial-assistant context item, not a fiction that the full answer was heard. |
| 7. Tool calls already started | Preserve the existing `tool_call` and effect events. Cancel only not-yet-started calls; an in-flight read may finish but its result is not used for a cancelled run. An in-flight side effect follows its delivery semantics and is never silently retried because speech was interrupted. |
| 8. Irreversible actions | Voice interruption cannot undo an external effect. Consequential tools remain staged by default and require an explicit, durable confirmation in a new Zeta turn; for ASR ambiguity, use a constrained confirmation such as "Yes, send it to Alice" and repeat material details. |

This differs from the Realtime conversation truncation rule. In a native
Realtime WebRTC session, the server can truncate audio based on actual played
buffer state. Here Zeta's semantic history and TTS playback history are
separate; segment-level audible records provide a conservative equivalent.

Recommended new durable records (raw microphone audio is ephemeral by default):

```text
voice.session.opened                # session id, ASR provider/session id; no API secret
voice.input.transcribed             # final transcript, ASR item id/language/model
zeta.user_message                   # the one authoritative textual user turn
voice.assistant.audible             # complete text segment confirmed played
voice.playback.interrupted          # run id, audible segment ids, reason
zeta.turn.failed                    # existing turn_aborted projection, reason=voice_interrupted
```

Partial ASR deltas, audio bytes, TTS byte chunks, and unplayed generated text
are transient. Optional audio retention is a separate consent, encryption, and
retention feature, not a default event-log payload.

## 6. Tool-call ownership

| Placement | Latency | Audit / replay / authorization | Verdict |
| --- | --- | --- | --- |
| Realtime model calls Zeta domain tools directly | One native loop after a function call. | Realtime selects calls; Zeta can audit execution but must mirror conversation and confirmation policy. | Reject for the primary Zeta voice path. |
| One `delegate_to_zeta` Realtime tool | Adds at least a Zeta model loop plus a Realtime response generation. | Zeta can execute tools, but Realtime still chooses delegation and speaks/interprets result. | Reject: semantic drift and latency. |
| Zeta alone selects and executes domain tools | Same as CLI; streaming text can begin before a final answer when no tool is needed. | Existing schemas, capability allow-lists, staging, effect keys, delivery semantics, and prompt trace all apply. | **Use this.** |
| Realtime/client owns only voice-session tools | Immediate local control. | It may stop audio, change device, or display transcript; it cannot take domain action. | **Use this limited scope.** |

The exact authority boundary is therefore:

```text
Zeta configured model: choose clarification, answer, and domain tool calls.
Zeta capability runtime: validate, authorize/stage, execute, journal effects.
Voice client/gateway: microphone, VAD, playback, ASR/TTS transport, cancellation signaling.
OpenAI Realtime transcription: ASR and endpointing only.
```

All consequential Zeta capabilities retain their normal delivery semantics and
idempotency keys. In particular, a spoken acknowledgement cannot be evidence
that an effect happened; only the tool/effect events are. This keeps voice
replayable and consistent with Commas `ask`/`propose`/`do` workflows.

## 7. Recommended architecture

### State and continuity

The voice client selects an existing Zeta `session_id`; for Commas continuity
it uses the same state directory and `COMMAS_SESSION_ID` value. The gateway
creates a `RuntimeContext` via `session_for_id()` and submits the final
transcript through the session-turn path. CLI and voice therefore read the same
timeline, prompt objects, tool results, and turn records.

OpenAI Realtime session IDs, ephemeral client secrets, ASR partials, and media
buffers are transport state. Persist only non-secret correlation IDs and final
outcomes needed to audit a voice turn. A new browser page can create a fresh ASR
session without losing Zeta continuity.

### Transports

```text
1. browser -> Zeta gateway: authenticated WebSocket control channel
2. browser <-> OpenAI: WebRTC transcription session using a gateway-minted
   ephemeral client secret; OpenAI API key stays server-side
3. browser -> gateway: final transcript / VAD speech-start notification
4. gateway -> browser: Zeta run progress, tool status, and TTS PCM bytes
```

Use WebRTC for microphone media because that is OpenAI's browser/mobile
recommendation. The gateway may alternatively proxy a WebSocket media pipeline
for a server-side device, but it should not proxy browser PCM merely to obtain
control events. [Realtime WebRTC](https://developers.openai.com/api/docs/guides/realtime-webrtc)

### Voice state machine

```text
IDLE
  -> LISTENING              browser opens ASR session
  -> SPEECH                 VAD speech_started
  -> ENDPOINTING            VAD stopped / explicit commit
  -> DISPATCHING            final transcript persisted; Zeta run id allocated
  -> GENERATING             configured Zeta model and tools
  -> SPEAKING               text segments -> TTS -> playback
  -> IDLE                   final answer spoken

SPEAKING or GENERATING --speech_started--> INTERRUPTING
INTERRUPTING -> SPEECH     playback stopped, TTS aborted, run cancellation requested
INTERRUPTING -> IDLE       cancellation completes with no new utterance
```

Only `DISPATCHING` commits a user message. Only a completed Zeta model event
commits a complete assistant message. `SPEAKING` additionally commits the
audible segment ledger needed for interruption context.

## 8. Smallest useful prototype

Build a local browser harness, not a mobile app and not an OpenAI redesign.
It should intentionally use a real microphone and real OpenAI ASR/TTS, but may
use whichever model Zeta is already configured to use.

### Components and integration points

| Component | Responsibility | Existing integration point |
| --- | --- | --- |
| `zetad.voice` gateway (new) | Session selection, token mint endpoint, browser control WebSocket, run registry, TTS proxy, event persistence. | `RuntimeContext` / `session_for_id()` and `run_session_request()`; do not fork a new agent loop. |
| Local web harness (new) | WebRTC microphone track, ASR event handling, partial transcript UI, PCM playback, immediate stop, control WebSocket. | Uses gateway protocol only. |
| Progress subscription (new/refactor) | Delivers `runtime.stream.chunk`, tool-start/result, final/aborted notifications per `run_id`; remains transient. | `ModelTurnStreamSink` in `zeta.run.streaming`, `run_agent()` and `zetad.rpc.routes.route_run()`. |
| Abortable model transport (new/refactor) | Makes `session.cancel` close the active provider stream and establishes a cancellation barrier before new tools/calls. | `ModelGateway.generate`, `chat_completions.py`, `responses.py`, and `zeta.run.runtime`. |
| Voice event projection (new) | Makes final transcript and conservatively audible partial assistant text visible to a following prompt. | `zeta.records.events`, `MODEL_TIMELINE_TYPES`, and `zeta.context.components`. |

The current RPC `RunState` is held per `RpcClient`; a browser gateway must not
depend on a short-lived stdio peer to cancel a run. Extract a server-owned
`RunRegistry` from [routes.py](../zeta/src/zetad/rpc/routes.py), then share it
between RPC and the voice gateway.

### Minimal control protocol

Browser-to-gateway WebSocket messages:

```json
{"type":"voice.configure","session_id":"default","language":"en"}
{"type":"voice.transcript.final","asr_item_id":"item_123","text":"Please inspect the failing test."}
{"type":"voice.barge_in","run_id":"run_123","played_segment_ids":["seg_1"]}
{"type":"voice.playback","segment_id":"seg_1","state":"completed"}
```

Gateway-to-browser messages:

```json
{"type":"voice.asr.partial","item_id":"item_123","text":"Please inspect"}
{"type":"voice.run.started","run_id":"run_123"}
{"type":"voice.text.delta","run_id":"run_123","text":"I will inspect"}
{"type":"voice.tool","run_id":"run_123","name":"read","state":"started"}
{"type":"voice.tts.segment","run_id":"run_123","segment_id":"seg_1","text":"I will inspect the failing test.","format":"pcm"}
{"type":"voice.run.terminal","run_id":"run_123","outcome":"completed"}
```

`voice.tts.segment` binary frames may carry PCM after the JSON header. The
gateway, not the browser, calls the Speech endpoint so no standard OpenAI key
is exposed. The ASR ephemeral secret is minted by the gateway with the
transcription session configuration and is short-lived.

### Test script

The prototype is complete enough only when one continuous session demonstrates:

1. Speak a request; view partial ASR text and commit one final `zeta.user_message`.
2. Verify the active Zeta profile (local, Codex, or other configured model) made
   the semantic response; start hearing its first complete sentence before the
   full answer is complete when that provider streams.
3. Ask a spoken clarification and verify the same session history supplies the
   answer.
4. Trigger one safe Zeta capability. A deterministic demo capability may be
   mocked, but it must be registered through the normal `CapabilityRegistry` so
   the test exercises tool selection, `tool_call`, and `tool_result` events.
5. Speak over the answer. Playback must stop locally; TTS must be aborted; the
   Zeta run must become `voice_interrupted`; no new tool call may begin after the
   cancellation barrier.
6. Continue by voice or `commas ask` with the same session ID and inspect the
   event log/prompt trace. It must show the final transcript, tool trail,
   interruption, and only the segments actually heard.

Use real OpenAI Realtime transcription and Audio Speech. Mock only a harmless
domain capability and, if necessary, a non-streaming local model during UI
development. Do not mock VAD, cancellation, or real TTS in the acceptance run.

### Falsifying experiments

- **Cancellation:** if the new model adapter cannot terminate the configured
  provider request promptly, or a model/tool starts after cancellation was
  observed, v0 does not meet interruption semantics. Keep push-to-talk and do
  not market barge-in until fixed.
- **Incremental TTS:** if the selected provider cannot expose usable text deltas
  or sentence segmentation produces unacceptable overlap/gaps, retain complete-
  answer TTS for v0 and treat low first-audio latency as later work.
- **Continuity:** if a Commas turn in the selected session does not appear in the
  following voice prompt (or vice versa), stop: session-id/state-dir routing is
  wrong.
- **Architecture C spike (optional, isolated):** configure a Realtime
  conversation with automatic responses disabled, insert supplied Zeta text,
  and attempt to obtain audio. It falsifies C if audio requires `response.create`
  and the output is not guaranteed verbatim/no-model reasoning. Do not add it to
  v0 even if a prompt appears to work once.

## 9. Required Zeta changes

1. For the recommended chained v0, add a voice gateway and authenticated
   browser control transport; reuse `RuntimeContext` and session-turn requests
   rather than creating a voice-only conversation implementation.
2. Extract RPC's run registry and add a transient progress sink. Forward model
   text deltas to subscribed clients without persisting chunks as timeline
   messages.
3. Make model generation and compatible capability execution abortable. Today
   `CancellationToken` is cooperative; realtime barge-in requires cancellation
   to propagate into the HTTP stream and a clear policy for uncancellable tools.
4. Add voice event schemas/projections for final ASR, audible segment ledger,
   and interruption. Update prompt assembly so a following turn receives only
   text conservatively known to have been heard.
5. Add sentence segmentation, ordered streaming TTS, playback acknowledgements,
   and a TTS cancel handle. Keep all audio/transcript partials out of durable
   history by default.
6. Add integration tests for same-session Commas/voice continuity, tool/event
   causality, cancellation-before-tool, interruption-during-playback, and
   unsafe-to-retry effects.
7. If Architecture A is deliberately offered later, add a provider-native
   `RealtimeLiveModelSession` that writes canonical timeline projections but
   never publishes `session.turn.requested`. It exchanges Realtime function
   calls with the capability executor and results back to Realtime; it does not
   start a second configured-model run.

## 10. Open questions

- Which configured Zeta providers support cancellation at transport level? The
  answer may vary: a local Chat Completions server and the Codex Responses
  backend should expose a common `cancel()` abstraction but may implement it
  differently.
- What VAD configuration works for Rémi's microphone, French/English code
  switching, and brief pauses? Compare `server_vad` and `semantic_vad` with a
  fixed evaluation corpus; do not choose from clean synthetic audio.
- How should session authorization map a browser identity to a Zeta session and
  to OpenAI's privacy-preserving safety identifier?
- Is segment-level audible history sufficient, or does a later product need
  word-aligned local playback accounting? The TTS docs do not provide a word
  timing contract, so avoid claiming exact character truncation.
- Should a voice request default to Commas `ask` semantics, or present an
  explicit workflow switch before entering `propose`/`do`? v0 should default to
  `ask` and make side-effect confirmation conspicuous.

## 11. Sources

All external claims above use current official OpenAI documentation/examples:

- [Realtime and audio](https://developers.openai.com/api/docs/guides/realtime)
- [Realtime conversations](https://developers.openai.com/api/docs/guides/realtime-conversations)
- [Realtime transcription](https://developers.openai.com/api/docs/guides/realtime-transcription)
- [Voice activity detection](https://developers.openai.com/api/docs/guides/realtime-vad)
- [Realtime API with WebRTC](https://developers.openai.com/api/docs/guides/realtime-webrtc)
- [Voice agents and Agents SDK voice abstractions](https://developers.openai.com/api/docs/guides/voice-agents)
- [Text to speech](https://developers.openai.com/api/docs/guides/text-to-speech)
- [Speech to text](https://developers.openai.com/api/docs/guides/speech-to-text)
- [Streaming Responses API output](https://developers.openai.com/api/docs/guides/streaming-responses)
- [Function calling](https://developers.openai.com/api/docs/guides/function-calling)
- [Realtime client-secret API reference](https://developers.openai.com/api/reference/resources/realtime/subresources/client_secrets/methods/create)

## Implementation sequence

### Exploratory spike

1. Add local browser microphone capture, a gateway-minted Realtime
   transcription session, and a final-transcript control message.
2. Submit that transcript into an existing Zeta session and inspect the shared
   CLI/voice event log.
3. Add one real Speech request and prove browser playback stop; run the isolated
   Architecture C falsification test without merging it into the path.

### v0

1. Extract server-owned run cancellation/progress plumbing and make configured
   model streaming abortable.
2. Implement sentence-segment TTS, audible segment events, interruption events,
   and conservative partial-context projection.
3. Ship the local web harness with read-only default workflow, one safe tool
   test, VAD/push-to-talk fallback, and integration tests.

### Later improvements

1. Measure and tune VAD, ASR delay, segmenting, TTS voice/style, and provider
   time-to-first-audio against a recorded voice evaluation set.
2. Add mobile/telephony transports, encrypted opt-in audio retention, richer
   confirmation UX, and provider-specific cancellation adapters.
3. Revisit native Realtime ownership only if Zeta intentionally accepts an
   OpenAI-owned semantic voice mode, or if OpenAI documents a true neutral
   realtime speech-rendering primitive.
