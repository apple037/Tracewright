# Private Multi-Model Customer Agent Design

**Date:** 2026-07-20

**Status:** Approved

**Scope:** Read-only customer-service recommendation MVP with high-risk human handoff

## 1. Objective

Build a local multi-model customer-service Agent Harness that accepts multi-turn customer messages, determines the customer's business intent and emotional context, retrieves verified evidence through RAG or read-only tool APIs, chooses a versioned response strategy, generates a response with private remote models, validates the response, and returns either a safe answer or a human-handoff notification.

The service must be diagnosable at node level. Every request must expose a trace that identifies the failed stage, failure reason, precise failure point, retry behavior, and fallback outcome. The system must never store or expose a model's hidden chain-of-thought. It stores structured decision summaries, evidence, reason codes, and model/tool metadata instead.

## 2. Confirmed Constraints

- Backend: Python 3.12, FastAPI, Pydantic v2.
- Package and virtual-environment management: `uv` with a committed `uv.lock`.
- Model service: private remote OpenAI/Ollama-compatible endpoint configured only through `.env`.
- Planned private-endpoint aliases, used as replaceable configuration rather than code-level requirements and not considered verified until the Model Inventory Gate succeeds:
  - `qwen3.6:35b-a3b` (expected upstream `Qwen/Qwen3.6-35B-A3B`): complex strategy and customer-facing generation, with thinking disabled.
  - `qwen3.5:9b` (expected upstream `Qwen/Qwen3.5-9B`): intent/emotion classification, structured extraction, Traditional Chinese verification, and independent secondary promotion judging.
  - `gemma-4-E4B-it` (expected upstream `google/gemma-4-E4B-it`): primary semantic response and promotion judge; `gemma-4-12B-it` (expected upstream `google/gemma-4-12B-it`) is the configured quality fallback if calibration is insufficient.
  - `qwen3:embedding:0.6b` (expected upstream `Qwen/Qwen3-Embedding-0.6B`): embeddings.
- Multiple model calls are allowed; larger models than the listed 35B-A3B are not required.
- Business data is not copied into local customer, product, order, or CRM master tables.
- Business facts come only from RAG or read-only tool APIs. MVP integrations use typed mock adapters.
- MVP returns advice only. It cannot create orders, appointments, refunds, or CRM updates.
- High-risk situations are handed to a person through a signed generic Webhook.
- Entry points: REST API and a lightweight browser Test Console.
- Every customer/session-scoped request is authorized against an authenticated principal and tenant. A request body identifier is never accepted as authority to access another customer's context or tools.
- Performance is measured as a baseline. The initial release has no hard latency gate.
- Local/demo deployment uses Docker Compose with PostgreSQL/pgvector, migration, application, worker, and demo frontend services. Remote model servers remain external and replaceable.
- Conversation text, including high-risk turns, is retained for 30 days without masking by explicit product risk acceptance. Structured trace/audit records are retained for 180 days. Access controls and data-location rules below still apply.
- Passwords, API keys, Authorization headers, Cookies, and database connection strings are never logged.

## 3. Architecture Decision

Use a **Local Multi-Model Agent Harness** implemented as a **Fixed Turn Pipeline + Typed Nodes + Model Advisors**. The MVP does not implement a reusable orchestrator, workflow DSL, dynamic graph, plugin system, or LangGraph runtime.

The Harness has two isolated execution surfaces:

- **Runtime Harness:** serves customer turns, executes retrieval/tools, validates responses, records traces, and creates handoff events.
- **Evaluation Harness:** replays versioned datasets and failure clusters, evaluates candidates, runs deterministic gates and independent model judges, and records promotion evidence. It cannot activate a candidate directly.

Both surfaces reuse typed node contracts, model adapters, and trace schemas, but use separate concurrency budgets and entry points so offline evaluation cannot starve interactive traffic. "Harness" describes the product boundary; it is not a generic agent framework in the MVP.

Each node has one responsibility, consumes and produces Pydantic models, and records its own span. `TurnPipeline` controls retries, skips, fallbacks, and handoff through explicit Python control flow. Models can classify or generate only inside their assigned node; they cannot choose arbitrary actions or write business data.

### 3.1 Request State Flow

1. `input_gate`: validate input size, prompt-injection indicators, and an `AuthenticatedPrincipal`; bind tenant, customer, session, and case identifiers before any context/RAG/tool access; create `trace_id` and request context.
2. `context_loader`: load the retained conversation window and applicable policy versions.
3. `dialogue_classifier`: make one 9B structured-model call that produces business intent, conversation mode, urgency, language, and the `EmotionAssessment` defined below.
4. `risk_precheck`: apply deterministic high-risk rules before retrieval or generation.
5. `evidence_planner`: determine required RAG collections and read-only tools with typed parameters and freshness requirements.
6. `evidence_collector`: call independent RAG/tool sources concurrently with bounded timeouts and retries.
7. `evidence_validator`: verify source, freshness, required fields, conflicts, and sufficiency. The system does not guess when evidence is insufficient.
8. `strategy_selector`: combine policy, intent, conversation mode, emotion, risk, and evidence into a versioned `StrategyDecision` with reason codes.
9. `response_generator`: use the 35B-A3B model with thinking disabled to generate a response from verified evidence and the selected strategy.
10. `response_validator`: run deterministic format/policy checks and the configured Gemma primary semantic judge for grounding, citations, tone, route, and risk. Every `zh-TW` response also requires an independent Qwen Chinese-verifier verdict.
11. `response_repair`: if the draft is repairable, request one constrained rewrite that addresses only listed failures, then validate once more.
12. `finalizer`: return a validated response, a conservative factual fallback, or a safe handoff message.
13. `handoff_notifier`: create an outbox event and deliver a signed Webhook without blocking the safe customer response.

Every transition records `started`, `completed`, `failed`, or `skipped`. Repair is limited to one attempt to prevent loops.

### 3.2 TurnPipeline Responsibilities

`TurnPipeline` is an application service with explicit Python calls and branches. It enforces node order, request deadlines, call budgets, cancellation, retry limits, idempotency, fallback, and handoff. It preserves request-scoped state and short-lived strategy state such as “do not probe this direction for the next two turns.” It writes a span for every node and concurrency wait.

Models are advisors. They return typed classifications, strategy proposals, drafts, or verdicts. They cannot add nodes, reorder the fixed pipeline, call undeclared tools, execute business actions, override hard risk policies, or retry themselves indefinitely.

Independent RAG and tool calls run concurrently inside `evidence_collector`. Required-source failure cancels unnecessary remaining work. `TurnPipeline` passes the combined evidence to the next node only after validation.

## 4. Conversation Mode, Emotion, and Strategy

Emotion analysis is an MVP feature but cannot override the customer's actual business request.

The MVP does not have a separate emotion-classifier node or model role. `dialogue_classifier` returns intent and emotion in one typed response to avoid duplicate inference and inconsistent labels. A separate role is added only if later evaluation demonstrates that a dedicated model materially improves the baseline enough to justify another call.

### 4.1 Conversation Mode

The 9B classifier selects one of:

- `informational`: product, policy, warranty, or explanatory request.
- `transactional_read`: order, stock, account, or status lookup that remains read-only.
- `complaint`: dissatisfaction requiring acknowledgment and possible handoff.
- `emotional_support`: the user primarily wants acknowledgment or to talk.
- `casual`: ordinary social conversation without a business task.
- `boundary`: denial, refusal to continue, or a request to stop probing.
- `unknown`: insufficient evidence to classify safely.

### 4.2 EmotionAssessment Contract

`EmotionAssessment` contains:

- `category`: `self_doubt`, `insecurity`, `grief_loss`, `stress_exhaustion`, `fear_avoidance`, `positive_shift`, `neutral`, or `unknown`.
- `dialogue_stage`: `surface`, `middle`, `deep`, `positive_close`, or `not_applicable`.
- `override`: `denial`, `boundary`, `positive_close`, `humor_or_challenge`, `no_emotional_content`, `explicit_positive`, `light_topic`, or `none`.
- `response_mode`: `natural_follow`, `brief_acknowledgment`, `open_probe`, `direct_label`, `quote_and_label`, or `business_first`.
- `confidence`: number from 0 through 1.
- `evidence_spans`: short exact excerpts from the user-visible conversation that support the classification.
- `reason_codes`: finite machine-readable codes; never hidden reasoning text.

Low confidence, conflicting signals, or absent evidence produce `unknown`; the service does not force an emotion label. Override detection is semantic and uses prior-turn context. A bare keyword such as “沒有” or “不是” is not sufficient to classify denial.

### 4.3 Strategy Profiles

- Informational and transactional replies answer verified facts first and add only a proportionate acknowledgment. The companion persona's two-sentence limit does not apply globally.
- Complaints acknowledge the concrete inconvenience, state verified facts, and hand off when a risk rule requires it.
- Emotional-support and casual replies can use the five response modes derived from `skill-tuning` v29 while avoiding repetitive patterns and unsolicited advice.
- Boundary states stop the current probing direction; an explicit boundary is remembered for the following two turns.
- High-risk states bypass open-ended generation decisions and create a handoff event.

## 5. Flexible Model Registry and Routing

Application code addresses model roles, never vendor or model names. The stable roles are:

- `dialogue_classifier`;
- `strategy_advisor`;
- `response_generator`;
- `response_judge`;
- `response_judge_zh_verifier`;
- `promotion_judge_primary`;
- `promotion_judge_secondary`;
- `embedding`.

Each role resolves to a named profile in `config/models.yaml`. The versioned YAML contains no secrets. It defines adapter type, model identifier, capabilities, generation parameters, timeout, concurrency/admission limits, optional fallback profiles, and model-specific request options. `.env` provides endpoint URLs, credentials, configuration path, and optional per-role overrides.

Configuration precedence is:

1. explicit environment override for a role/profile field;
2. `config/models.yaml`;
3. safe application defaults that contain no endpoint or credential.

An example configuration is:

```yaml
endpoints:
  private_chat:
    adapter: openai_compatible
    base_url_env: PRIVATE_CHAT_BASE_URL
    api_key_env: PRIVATE_CHAT_API_KEY
    max_concurrency: 6

profiles:
  fast_structured:
    endpoint: private_chat
    model: qwen3.5:9b
    capabilities: [chat, structured_json, reasoning_toggle]
    request_options:
      enable_thinking: false
    temperature: 0
    max_concurrency: 3

  quality_generator:
    endpoint: private_chat
    model: qwen3.6:35b-a3b
    capabilities: [chat, reasoning_toggle]
    request_options:
      enable_thinking: false
    temperature: 0.2
    max_concurrency: 2

  gemma_judge:
    endpoint: private_chat
    model: gemma-4-E4B-it
    capabilities: [chat, structured_json, reasoning_toggle]
    fallback_profiles: [gemma_judge_12b]
    request_options:
      enable_thinking: false
    temperature: 0
    max_tokens: 512
    max_concurrency: 2

  gemma_judge_12b:
    endpoint: private_chat
    model: gemma-4-12B-it
    capabilities: [chat, structured_json, reasoning_toggle]
    request_options:
      enable_thinking: false
    temperature: 0
    max_tokens: 512
    max_concurrency: 1

  semantic_embedding:
    endpoint: private_chat
    model: qwen3:embedding:0.6b
    capabilities: [embedding]
    max_concurrency: 4
    batch_size: 32

roles:
  dialogue_classifier: fast_structured
  strategy_advisor: quality_generator
  response_generator: quality_generator
  response_judge: gemma_judge
  response_judge_zh_verifier: fast_structured
  promotion_judge_primary: gemma_judge
  promotion_judge_secondary: fast_structured
  embedding: semantic_embedding
```

Users may replace every example model with another small/private model by changing configuration. A replacement is accepted only when its declared capabilities satisfy the role. Fallback chains are explicit; a missing role never silently inherits another profile.

The adapter interface supports OpenAI-compatible chat/embedding endpoints first. Provider-specific details such as Ollama-style thinking controls or extra request bodies live in the adapter/profile, not in `TurnPipeline`. Logs record the resolved role, profile, model, adapter, and configuration checksum but never credentials.

Before any live-model integration is accepted, a **Model Inventory Gate** queries the configured endpoint's model-list API (`/v1/models`, Ollama-compatible tags, or an adapter-specific equivalent), requires an exact configured alias match, and records the server-reported identifier/digest when available. It then runs bounded sample calls for chat, structured JSON, thinking disable behavior, and embeddings. There is no fuzzy alias matching or silent substitution. Until the private `.env` is supplied and this gate passes, these names remain planned aliases and development uses contract-test fakes/mocks.

The same capability probes run at startup to verify configured model availability, structured JSON behavior where required, reasoning/thinking disable behavior where declared, and embedding dimension. A capability failure either activates an explicitly configured compatible fallback or makes readiness fail. The service embeds a sentinel string and validates its vector dimension against the migration setting before allowing vector writes. The resolved alias, reported upstream identifier/digest, capability result, and configuration checksum are visible in readiness and the Demo Console without credentials.

The initially tested routing uses Qwen for classification, Gemma as the primary semantic judge, Qwen as the independent Traditional Chinese verifier and secondary promotion judge, a larger but bounded Qwen model for complex strategy/generation, and a dedicated embedding model. Model size and vendor names are not embedded in routing logic. Deterministic rules remain authoritative for high-risk and executable-action boundaries.

For runtime validation, non-`zh-TW` responses require deterministic gates plus the Gemma verdict. Every `zh-TW` response requires deterministic gates, an independent Gemma verdict, and an independent Qwen verdict over the same frozen draft and evidence. Neither judge sees the other's output. Both model verdicts must pass before publication. A disagreement on a repairable tone, completeness, or language issue enters the single allowed repair cycle; a disagreement involving risk, unsupported facts, unsupported commitments, citation validity, or tool/evidence grounding causes human handoff. A second disagreement after repair also causes handoff.

Each verdict records language, judge role, resolved model profile and checksum, criteria results, confidence, evidence references, bounded decision summary, parsing/repair events, and latency. It does not store hidden chain-of-thought. The Console identifies the exact judge and failed criterion rather than reporting a generic validation failure.

### 5.1 Concurrency, Admission, and Background Jobs

Use three small, concrete concurrency mechanisms:

1. **In-request async fan-out:** `asyncio.TaskGroup` runs independent RAG and read-only tool calls concurrently. Each child has its own timeout, retry policy, cancellation state, and span.
2. **Interactive admission and semaphores:** one application in-flight limit and an `asyncio.Semaphore` per model profile bound concurrent calls. Semaphore acquisition has a timeout and exposes a waiting-count metric. Saturation returns HTTP 429 with `Retry-After`; the MVP does not implement a standalone interactive queue service or scheduling framework.
3. **PostgreSQL durable jobs:** notification delivery, RAG ingestion/re-embedding, retention, and baseline evaluation run in background workers using `FOR UPDATE SKIP LOCKED`.

Initial safe values are configuration defaults, not performance promises:

```env
APP_MAX_IN_FLIGHT_TURNS=16
MODEL_ACQUIRE_TIMEOUT_MS=5000
MODEL_ENDPOINT_MAX_CONCURRENCY=6
FAST_STRUCTURED_PROFILE_MAX_CONCURRENCY=3
GENERATOR_PROFILE_MAX_CONCURRENCY=2
GEMMA_JUDGE_PROFILE_MAX_CONCURRENCY=2
GEMMA_JUDGE_12B_PROFILE_MAX_CONCURRENCY=1
EMBEDDING_PROFILE_MAX_CONCURRENCY=4
EMBEDDING_BATCH_SIZE=32
```

Profile/YAML values can replace these profile defaults. Profile limits are independent ceilings, not reserved capacity, so their sum is not expected to equal the endpoint limit. Every request must acquire both its profile semaphore and the shared endpoint semaphore; effective concurrency is bounded by the smaller available capacity, and the endpoint-wide limit always wins when profiles share a server. No profile example may exceed its endpoint cap. The Console and README display both limits and identify which semaphore caused a wait. Trace data includes `wait_started_at`, `wait_ms`, `wait_limit_kind`, `started_at`, execution latency, deadline, and cancellation reason. Disconnects and expired deadlines cancel work that has not acquired capacity.

Do not add Redis, RabbitMQ, Kafka, Celery, a generic queue abstraction, or a replaceable scheduling framework for the MVP. Background workers call a small PostgreSQL job repository directly. Introduce a queue abstraction only if a second backend is actually required.

## 6. High-Risk Handoff

A handoff is triggered by any of:

- low confidence below the configured release threshold;
- contradictory or insufficient evidence for a required factual answer;
- price, contract, refund, legal, delivery, or other unsupported commitment;
- complaint, threat, self-harm, unlawful request, account security, or sensitive personal-data issue;
- explicit customer request for a person;
- repeated model, RAG, database, or tool failure;
- validator failure that remains after one constrained repair.

`HandoffEvent` contains the customer identifier, trace ID, case/session ID, risk level, reason codes, summary, recommended next action, timestamp, and idempotency key. Customer and conversation values are not masked by product decision; secrets are always excluded.

The Webhook uses an HMAC signature, bounded timeout, idempotency key, and outbox retries. Status is `queued`, `delivering`, `delivered`, or `failed`. The customer is not told that a person has accepted the case unless the downstream system explicitly confirms it.

High-risk classification and retention are separate decisions: self-harm, account-security, or unexpectedly sensitive personal-data content triggers safer handling but does not silently bypass the confirmed 30-day raw-text retention rule. Raw conversation text is confined to tenant-scoped `runtime.turns`; structured stdout logs and observability events reference the turn and store bounded summaries rather than duplicating full conversation text. Database connections require TLS where supported, storage uses platform volume/disk encryption, customer-text access requires least privilege and tenant authorization, and every Console/API access to raw turns is audit logged. Raw turns are excluded from improvement prompts and datasets unless a human explicitly reviews and selects them. Secrets and authentication material remain prohibited regardless of the no-masking decision.

## 7. API and Test Console

### 7.1 REST API

- `POST /api/v1/turns`: accepts `message`, optional/new `session_id`, optional `case_id`, and `customer_id` only for a principal with `customer:act_as`; self-service customer identity is derived from authentication. It returns `reply`, `trace_id`, citations, conversation mode, and handoff status.
- `GET /api/v1/traces/{trace_id}`: returns the ordered node waterfall with reasons, precise failure points, retries, and fallback results.
- `GET /api/v1/traces/{trace_id}/events?after_sequence={n}`: returns ordered events after a sequence number so the Console can incrementally poll an active trace without WebSocket/SSE infrastructure.
- `POST /api/v1/traces/{trace_id}/retry`: admin-only manual retry for a terminal trace. It requires a reason, creates a new full-turn execution linked to the original trace, and returns the new `trace_id`; it never mutates or resumes the original trace.
- `GET /api/v1/health`: returns liveness/readiness for PostgreSQL, pgvector, model roles, RAG, tools, and notification delivery.
- `GET /console`: serves the lightweight internal Test Console.

The chat API uses a configured bearer token outside development. Authentication produces an `AuthenticatedPrincipal` with `subject_id`, `tenant_id`, roles/scopes, and permitted customer identity. For a self-service principal, the server derives `customer_id` from the principal and rejects any conflicting request value. A service-agent principal may supply `customer_id` only with `customer:act_as` scope and only for a customer in the same authorized tenant. `session_id` and `case_id` must belong to the bound tenant/customer or be created under that binding; callers cannot attach an existing identifier from another customer. The resulting immutable `AuthorizedCustomerContext` is passed to context loading, RAG filters, every tool call, trace lookup, and manual retry. Adapters must not accept an unbound raw `customer_id`.

Authorization failures occur before model, RAG, or tool calls and return the configured non-enumerating 404/403 policy. Mock adapters and demo authentication enforce the same ownership checks. Trace endpoints and manual retry require internal/admin scope plus tenant access; an admin token is not implicitly cross-tenant. The Console reads tokens from server-side configuration and never embeds model or database credentials in client JavaScript.

### 7.2 Incident-First Demo Console

Use the approved **Incident-first** layout. The default view answers three questions before showing raw logs: what failed, why it failed, and what the system did next.

The screen contains:

1. **Trace header:** trace/session ID, overall status, language, start time, total duration, active configuration checksum, final reply/handoff state, and copy-trace-link action.
2. **Issue summary card:** deterministic `IssueSummary` with severity, failed node, component/operation, error code, plain-language explanation, precise failure point, customer impact, retry/repair outcome, and final fallback/handoff action. It links to the primary failure event.
3. **Horizontal pipeline:** ordered typed nodes run left to right with connectors and horizontal overflow on narrow screens. Each node shows icon/text status, short name, duration, and attempt count. The selected node has a high-contrast outline; failed nodes remain visibly marked even when not selected.
4. **Details below the flow:** clicking a pipeline node selects it and opens one full-width detail panel directly below the horizontal flow. The panel uses predictable sections: purpose/status/timing, typed input summary, typed output summary, structured decision and reason codes, evidence/tool/model dependencies, attempts and retries, errors/fallbacks, and child events. Empty sections are omitted. The failed field or operation is highlighted and linked to the exact event. If the trace has errors, the primary causal failure node is selected and its detail panel is open by default. If there is no error, no detail panel is initially open. Clicking the selected node again collapses the panel; pinning opens comparison cards inside the same lower panel rather than expanding the horizontal row vertically.
5. **Event explorer:** chronological event list on the left and selected-event details on the right. Decision, model, tool, validation, retry, repair, and notification events use distinct typed renderers instead of displaying undifferentiated JSON.
6. **Payload disclosure:** concise human-readable fields appear first; request/response summaries, evidence references, model/profile/checksum, tool parameters/results, and versioned JSON payloads are expandable. Hidden chain-of-thought and secrets never appear.
7. **Conversation and evidence context:** the relevant customer turn, generated draft, final response, cited evidence, tool result, and judge verdicts can be compared without leaving the trace.
8. **Filters:** trace/session ID, status, node, component, event type, error code, judge role, model profile, and time range. A `Failures only` toggle is available by default.

Node expansion uses a shared shell with type-specific content:

- classification nodes show labels, confidence, evidence spans, overrides, and reason codes;
- evidence nodes show planned sources, parallel child calls, freshness, conflicts, sufficiency, and citation mappings;
- strategy nodes show the selected strategy/version, applicable policy rules, rejected alternatives by reason code, and response constraints;
- generation nodes show model/profile, prompt/template version, bounded parameters, token/latency data, evidence IDs supplied, and generated draft;
- validation nodes show deterministic checks and separate Gemma/Qwen criterion matrices, disagreements, failed fields, and repair instructions;
- handoff nodes show trigger reasons, outbox state, attempts, idempotency key, signature metadata without secrets, and downstream response.

Input/output summaries come from explicit per-node presenter functions over typed contracts; the frontend does not infer meaning from arbitrary JSON. Large text and arrays are collapsed with item counts, searchable, and available in a final raw-data disclosure. A node deep link includes `trace_id`, `span_id`, and optional `event_sequence`, so a copied link opens the same expanded context.

The hierarchy is progressive:

- Level 1: issue, impact, and outcome;
- Level 2: pipeline and chronological events;
- Level 3: exact typed fields and expandable JSON.

The active trace polls the incremental event endpoint using the latest sequence number and exponential idle backoff. Polling stops when the trace reaches a terminal state or the page is hidden. Each update preserves the selected event and scroll position. The MVP does not use WebSocket or SSE.

Selected-node/detail-panel state is client-side UI state and is preserved across incremental polling. Automatic error focus runs only on initial trace load, when changing trace, or when the currently viewed running trace first enters a failed terminal state; it never repeatedly steals focus after the user manually selects another node. Nodes support mouse, touch, `Enter`, and `Space`, expose `aria-selected`/`aria-expanded`/`aria-controls`, and keep the error state visible while the detail panel is closed.

### 7.3 Automatic and Manual Retry

Automatic retry is allowed only for explicitly classified transient failures: dependency timeout, connection reset, HTTP 408/429, selected 5xx responses, PostgreSQL serialization/deadlock errors, and one malformed structured-model response repair. Validation, safety, insufficient-evidence, unsupported-claim, authentication/authorization, contract 4xx, and deterministic policy failures are not retried automatically.

Each component declares `max_attempts`, per-attempt timeout, total retry budget, retryable error codes, and capped exponential backoff with jitter. The default is one attempt; remote model/tool calls may use at most three total attempts when their profile enables retry. The total turn deadline always wins. Every attempt records `attempt_id`, start/end time, wait/backoff, error code, dependency request ID when available, and outcome under the same span. Exhaustion records `retry_exhausted` and the next fallback/handoff action.

Manual retry uses full-turn replay only. The Console shows `Retry entire turn` for authenticated admins when the trace is terminal. A confirmation dialog requires a human reason and explains that the retry:

- creates a new trace with `retry_of_trace_id`, initiator, reason, and retry sequence;
- reuses the original customer message, captured conversation-context snapshot, and prompt/policy/model/RAG artifact versions;
- performs fresh read-only tool calls so transient or time-varying business data can recover;
- does not automatically send a second customer reply; the replay result is marked `review_required` in the Demo Console;
- does not create a duplicate handoff notification when the original idempotency key already has a queued or delivered outbox record.

Manual retry is rejected for running traces, missing/expired input snapshots, unauthorized users, exceeded per-trace retry limit, or artifact versions that can no longer be resolved. The default manual limit is three retries per root trace and is configurable. A retry chain is displayed as `root → retry 1 → retry 2`; each trace remains immutable and independently diagnosable. Notification delivery has its own outbox retry controls and is not implemented by replaying the customer turn.

Status never relies on color alone: every state has text and an icon. Failed nodes use high-contrast borders and labels; selected nodes remain readable in dark and light browser themes. Timestamps display local time with the original ISO value available on hover. Durations use consistent milliseconds/seconds formatting.

`IssueSummary` is computed from typed failures and terminal outcomes, not generated by an LLM. If several failures occur, the Console identifies the primary causal failure and separately lists downstream/cancellation events. The user can copy a redacted-by-secret-policy trace JSON bundle for debugging; ordinary customer data remains unmasked by the confirmed retention policy.

Other Console capabilities remain multi-turn chat, preset cases, citations, dependency/model health, emotion-baseline labeling, and read-only improvement history. Candidate approval, activation, and rollback use authenticated CLI commands in the MVP.

Use server-rendered static HTML/CSS/JavaScript for the MVP; do not add a frontend framework or charting dependency.

## 8. PostgreSQL and pgvector

Use one PostgreSQL service with separate schemas:

- `rag.documents`: document identity, source, version, checksum, valid dates, access metadata, and ingestion state.
- `rag.chunks`: document reference, ordinal, content, metadata, embedding, and created timestamp. Use exact cosine search in the MVP; add HNSW only after measured corpus size or latency requires it.
- `runtime.conversations`: session identity and expiry.
- `runtime.turns`: user/assistant text, citations, trace reference, and expiry. Raw conversation retention is 30 days.
- `observability.traces`: request-level status, timestamps, terminal outcome, optional primary failure event reference used to build the deterministic `IssueSummary`, and immutable retry lineage (`root_trace_id`, `retry_of_trace_id`, `retry_sequence`, initiator, reason, delivery disposition).
- `observability.spans`: node-level parent/child relationships, status, duration, attempts, and failure location.
- `observability.events`: monotonically sequenced typed decision, model-call, tool-call, validation, retry/repair, and notification events. Common indexed columns hold sequence, event type, component, status, error code, and timestamps; versioned JSONB payloads hold type-specific fields. This avoids separate model/tool tables until query volume proves they are needed.
- `notification.outbox`: signed handoff payload, idempotency key, attempts, next-attempt time, and delivery state.
- `runtime.jobs`: durable background work with type, priority, payload, status, available time, attempts, lock owner/time, idempotency key, and last precise error location.
- `improvement.iteration_events`: append-only lifecycle records for improvement candidates, tests, human approval, activation, rejection, and rollback.

Embeddings are stored only in pgvector columns. Trace and outbox data use ordinary PostgreSQL columns and JSONB where schema evolution is necessary. Database changes are explicit Alembic migrations; the runtime never silently creates or alters production tables.

Retention jobs delete expired turns after 30 days and structured observability records after 180 days. RAG documents follow their version and validity policy rather than conversation-log retention.

## 9. RAG and Tool Boundaries

`RagClient.search(RagSearchRequest) -> RagSearchResult` and `ToolClient.call(ToolCallRequest) -> ToolCallResult` are stable interfaces. Mock and remote implementations must satisfy the same contracts.

Every evidence item includes source ID, version, retrieval timestamp, effective/expiry time when applicable, score, and content checksum. The validator rejects expired, conflicting, or incomplete evidence for claims that require it.

Tool APIs are read-only. Each tool declares allowed operations, parameter schema, response schema, timeout, retry policy, and freshness policy. HTTP 4xx is not retried except 408/429; timeouts and transient 5xx use at most two attempts with bounded backoff.

## 10. Observability and Error Location

Each request has `trace_id`; each node has `span_id`, `parent_span_id`, `sequence`, and `node_name`. Events include:

- timestamps, duration, status, and attempt;
- input/output schema version and safe payload snapshot;
- model/tool route and reason codes;
- policy/prompt/template/model versions;
- confidence, validation failures, risk flags, and fallback path;
- token and latency data;
- notification result.

Errors use a typed `AgentError` containing:

- `error_code`, `category`, `retryable`, and safe public message;
- `failure_stage`, `component`, and `operation`;
- optional `field_path` for contract/data errors;
- optional dependency name and endpoint path for external failures;
- exception type, source file, and source line in development/internal traces only;
- full stack trace in server logs only, never in the customer response.

Structured logs are emitted as JSON to stdout. PostgreSQL is the queryable trace store. Rotating application log files, an OTLP exporter, and an OpenTelemetry collector are deferred until an operational need and destination exist.

The system never stores raw chain-of-thought. “Thinking log” means structured decision summary, selected/rejected reason codes, evidence references, and validation outcomes.

## 11. Reuse from Existing Projects

### 11.1 `customer-service-agent-skills`

Reuse directly:

- intent, interaction-style, evaluation, and knowledge reference content after versioning;
- deterministic mock order/product data and test scenarios;
- one-revision then conservative-fallback behavior;
- Test Console interaction concepts.

Adapt:

- `RoleConfig` into Pydantic Settings;
- `OpenAICompatibleClient` into an async pooled HTTP client with tracing;
- `TurnJudgment`, `ToolResult`, `ReplyAudit`, and `AgentTurnResult` into expanded Pydantic contracts;
- pgvector cosine-search concepts into the migrated `rag` schema; defer HNSW until measurement requires it;
- the handoff adapter into the signed Webhook/outbox implementation.

Do not copy directly:

- the monolithic `CustomerServiceAgent` runtime;
- mutable global `last_agent_log`, `recent_turns`, and `prior_replies`;
- global `AGENT` plus `ThreadingHTTPServer`;
- a shared synchronous database connection that silently disables itself;
- automatic long-term customer-memory extraction, because the new MVP is read-only.

The existing isolated suite passes 13 tests. Running it with real `.env` settings can block on external connections, so the new test configuration must use dependency injection and never load production endpoints by default.

### 11.2 `skill-tuning`

Adopt:

- v29 separation between a finite decision layer and a style/expression layer;
- five conversational response modes and ordered overrides;
- emotion taxonomy and RULER-based evaluation;
- deterministic compliance checks followed by a semantic judge;
- 35B-A3B no-thinking generation, 9B judging, and one retry.

Adapt or reject:

- do not impose the companion persona's two-sentence and “do not solve” goal on business/information replies;
- do not use keyword-only denial detection;
- replace PowerShell execution and `REASON:/REPLY:` parsing with Python and Pydantic JSON;
- do not store native thinking output;
- guard against observed 35B long-form essay failures using schema/output limits, deterministic checks, the configured Gemma semantic judge, one repair, and fallback.

## 12. Emotion Baseline Labeling

Create a 60-item human-labeled baseline in the Console. Items combine adapted `skill-tuning` scenarios with customer-service messages, complaints, factual queries, boundaries, neutral content, and high-risk examples.

For each item, show the customer message and only necessary preceding turns. Hide model output until the human submits labels. The human labels:

- `conversation_mode`;
- `emotion_category`;
- `dialogue_stage`;
- `override`;
- `high_risk`;
- optional note or `ambiguous`.

After submission, show model labels and differences. Prioritize low-confidence samples and disagreements for review. Save results to `datasets/emotion-baseline-v1.jsonl` with a manifest containing taxonomy version, creation date, label counts, and checksum.

After all labels are complete, create a stratified 42-item development set and an 18-item locked test set. Prompt and policy iteration use only the development set. The initial target is macro-F1 at least 0.80 on non-ambiguous locked items, with no missed safety override represented in the locked set.

This 18-item result is explicitly a **provisional regression tripwire**, not evidence of generalization or production-quality emotion accuracy. Reports always show item count, per-class support, confusion matrix, raw misses, and bootstrap confidence intervals; they do not present the threshold as a statistically strong claim. One safety miss fails the known-case gate, while zero misses means only that the small locked set passed. Before emotion quality can be described as production validated, expand the adjudicated evaluation corpus to at least 200 items with at least 20 non-ambiguous examples for every reported class, then re-establish thresholds from the larger set.

## 13. Offline Improvement and Promotion

Improvement is offline and requires human approval. Models may propose prompt, policy, threshold, RAG, or model-profile candidates; they cannot edit application code, apply database migrations, activate a version, or modify production behavior directly.

### 13.1 Candidate Sources

- low-confidence classifications and model disagreements;
- deterministic or semantic validator failures;
- constrained repairs and conservative fallbacks;
- RAG no-answer, stale-source, or conflict outcomes;
- model, tool, database, admission/concurrency, background-job, and Webhook failures;
- human handoffs and explicit negative feedback;
- errors in the labeled emotion baseline or golden regression sets.

Candidate records may store embeddings in pgvector so a reviewer can run similarity searches. Automatic clustering, automatic proposal generation, and cluster-management UI are deferred. Proposals reference trace IDs and reviewed summaries rather than copying unreviewed production conversations into system prompts.

### 13.2 Versioned Artifacts

Every candidate records immutable references and checksums for:

- prompt templates;
- strategy policies;
- risk rules and thresholds;
- RAG document versions;
- Model Registry configuration;
- development, locked-test, and golden-safety datasets;
- evaluation code and metric schema.

The candidate also records its parent version, rationale, source traces/clusters, proposer, before/after metrics, approver, activation time, and rollback target.

### 13.3 Append-Only Improvement Ledger

Use one append-only table:

```sql
CREATE SCHEMA IF NOT EXISTS improvement;

CREATE TABLE improvement.iteration_events (
  id BIGSERIAL PRIMARY KEY,
  iteration_id UUID NOT NULL,
  sequence INTEGER NOT NULL,
  event_type TEXT NOT NULL CHECK (event_type IN (
    'candidate_created', 'candidate_updated', 'evaluation_started',
    'evaluation_completed', 'adjudicated', 'rejected', 'approved', 'activated',
    'rollback_requested', 'rolled_back'
  )),
  status TEXT NOT NULL CHECK (status IN (
    'pending', 'running', 'passed', 'failed', 'needs_adjudication', 'rejected',
    'approved', 'active', 'rolled_back'
  )),
  parent_iteration_id UUID,
  rollback_target_id UUID,
  title TEXT,
  reason TEXT,
  change_types TEXT[] NOT NULL DEFAULT '{}',
  source_trace_ids UUID[] NOT NULL DEFAULT '{}',
  failure_clusters JSONB NOT NULL DEFAULT '[]',
  proposed_changes JSONB NOT NULL DEFAULT '{}',
  artifact_refs JSONB NOT NULL DEFAULT '{}',
  model_config_checksum TEXT,
  dataset_versions JSONB NOT NULL DEFAULT '{}',
  metrics_before JSONB NOT NULL DEFAULT '{}',
  metrics_after JSONB NOT NULL DEFAULT '{}',
  gate_results JSONB NOT NULL DEFAULT '{}',
  actor_type TEXT NOT NULL CHECK (actor_type IN ('system', 'model', 'human', 'worker')),
  actor_id TEXT NOT NULL,
  audit_trace_id UUID,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (iteration_id, sequence)
);
```

Allowed `event_type` values are `candidate_created`, `candidate_updated`, `evaluation_started`, `evaluation_completed`, `adjudicated`, `rejected`, `approved`, `activated`, `rollback_requested`, and `rolled_back`. An `adjudicated` event must have a human actor, the disputed criterion verdicts, and the same candidate/artifact checksums as the evaluation it resolves. Allowed actor types are `system`, `model`, `human`, and `worker`.

Application roles may insert events but cannot update or delete existing rows. Improvement records are retained indefinitely. They reference production traces rather than duplicating full conversations. A SQL view selects the latest event per iteration for Console display and activation checks.

Indexes cover `(iteration_id, sequence)`, `(status, created_at DESC)`, GIN `source_trace_ids`, and GIN `change_types`.

### 13.4 Mandatory Promotion Gate

Before activation, a candidate runs a versioned promotion suite against the exact artifact and configuration checksums that would be activated:

1. configuration/schema validation and startup capability probes;
2. affected unit and contract tests;
3. PostgreSQL migration compatibility and pgvector dimension checks;
4. full pipeline E2E tests with mock RAG/tools/models/Webhook;
5. a separate human-reviewed golden safety set with at least 30 high-risk examples spanning the configured trigger categories, requiring 100% high-risk handoff recall and zero unsupported commitments/actions;
6. intent and tool-route accuracy of at least 95%;
7. RAG citation precision of at least 95%;
8. the locked 18-item emotion test set with macro-F1 at least 0.80 and no missed represented safety override, treated only as a provisional known-case regression gate;
9. replay of source failure clusters plus the general regression set;
10. admission, concurrency, background-job, retry, idempotency, fallback, exact-error-location, and rollback tests;
11. opt-in live model smoke/evaluation when the candidate changes a model profile, prompt, or provider adapter.

Promotion semantic evaluation uses two independently configured model families:

1. Gemma is the **primary judge** and produces the canonical structured verdict, failed criteria, evidence references, confidence, and bounded decision summary.
2. Qwen is the **secondary judge** and independently evaluates the same frozen candidate output without seeing the Gemma verdict.
3. A deterministic hard-gate failure always fails the candidate, regardless of either model verdict.
4. If both judges pass, the candidate becomes eligible for human approval; model agreement never activates it automatically.
5. If the judges disagree, the evaluation status is `needs_adjudication` and a human must resolve the disputed criteria in an append-only `adjudicated` event. The system must not average scores into an automatic pass.
6. The exact judge profile, model identifier, prompt/schema version, configuration checksum, raw structured verdict, and parser/repair events are stored with the evaluation record.

Gemma judge adoption requires calibration against Traditional Chinese, safety, grounding, citation, and unsupported-commitment examples labeled by a human. False-pass rate is the primary selection metric. Start with the configured E4B instruct profile; switch the profile to the 12B instruct fallback only if it fails the calibration threshold. These identifiers are deployment aliases and remain replaceable through the Model Registry.

An `evaluation_completed` event contains suite version, source commit, dataset checksums, artifact checksums, per-gate results, metrics, failures, duration, and worker identity. Any failed hard gate makes the evaluation fail. Performance remains report-only until capacity baselines establish explicit limits, but catastrophic output, timeout, admission, semaphore, or background-job regressions are shown to the reviewer.

An `approved` event requires `actor_type = 'human'`. An `activated` event is accepted only when:

- the latest evaluation for the same iteration passed every hard gate, with any judge disagreement resolved by a later checksum-matched human `adjudicated` event;
- the evaluated artifact/configuration/dataset checksums still match the candidate;
- a later human approval exists for those same checksums;
- no later rejection or candidate modification invalidated the result.

Authenticated `uv run` CLI commands call one promotion service for approval, activation, and rollback. The service validates the append-only ledger and checksums, then activates in one transaction with an advisory lock. New turns resolve the new active version after commit; in-flight turns retain their captured version. Rollback inserts new `rollback_requested` and `rolled_back` events and atomically restores the recorded target version. A duplicate PostgreSQL trigger/function enforcement layer is deferred until deployment risk justifies it.

## 14. Docker Compose and README

### 14.1 Compose Services

- `postgres`: PostgreSQL with pgvector, persistent volume, readiness check, and no automatic public exposure outside development.
- `migrate`: one-shot Alembic upgrade; application services start only after it succeeds.
- `app`: FastAPI API, fixed `TurnPipeline`, health endpoints, and internal Trace API.
- `worker`: the same Python image with a different command; claims notification, RAG ingestion, retention, evaluation, and improvement jobs through PostgreSQL `SKIP LOCKED`.
- `frontend`: separate lightweight demo frontend container that proxies same-origin API/Console requests.
- `demo-seed`: optional Compose profile that loads mock tool fixtures, example RAG documents, and the 60 labeling candidates.

Remote model services are external to Compose. Containers receive endpoint/config paths through `.env` and `config/models.yaml`. Compose includes explicit networks, named volumes, health checks, restart policy, read-only mounts where possible, and a non-root application user. Images are pinned to versions/digests during implementation rather than using floating `latest` tags.

Add `Dockerfile`, `compose.yaml`, `.dockerignore`, `.env.example`, `config/models.example.yaml`, and documented development/production override files. Secrets are never baked into images or committed.

### 14.2 README Contents

The root `README.md` must document:

1. purpose, read-only limitations, architecture, fixed `TurnPipeline`, and trust boundaries;
2. Docker/Compose prerequisites and optional local `uv` development prerequisites;
3. quick start, configuration copy steps, migration, demo seed, and shutdown;
4. every `.env.example` system, admission/concurrency, database, Webhook, authentication, retention, and observability setting;
5. Model Registry roles, profiles, capabilities, fallback chains, adapter options, and environment override precedence;
6. replacing classifier, judge, generator, strategy, and embedding models without code changes;
7. Compose services, profiles, ports, networks, volumes, health checks, and external model connectivity;
8. pgvector schema, migrations, embedding-dimension probe, ingestion, re-embedding, and backup/restore;
9. mock data and demo frontend workflows;
10. REST contracts, authentication, citations, trace lookup, and signed Webhook validation;
11. async fan-out, admission/semaphore limits, HTTP 429, PostgreSQL jobs, and worker recovery;
12. structured thinking/decision summaries, model/tool logs, precise failure locations, retention, and stdout log collection;
13. the 60-item human-labeling workflow, dataset split, metrics, and locked-test rule;
14. improvement candidates, append-only ledger, promotion tests, human approval, activation, and rollback;
15. unit, integration, E2E, failure-injection, promotion, and opt-in live-model commands;
16. troubleshooting for model connectivity, structured JSON, vector dimension, migrations, admission saturation, stuck PostgreSQL jobs, RAG freshness, tool timeout, and Webhook failure;
17. a production-readiness checklist.

## 15. Testing and Acceptance

### 15.1 Test Layers

- Unit tests for every node, Pydantic contract, strategy rule, risk rule, and error mapping.
- Contract tests shared by mock and remote RAG/tool/model/Webhook adapters.
- Model Inventory Gate tests for exact alias resolution, missing/duplicate alias, reported model digest, structured JSON, thinking disable behavior, and embedding dimension; live execution is opt-in until private `.env` exists.
- Authorization tests for self-service identity derivation, `customer:act_as`, tenant/customer/session/case ownership, trace access, manual retry, and IDOR attempts that assert no downstream model/RAG/tool call occurs.
- PostgreSQL integration tests for migrations, vector dimension, exact cosine search, retention, and outbox delivery.
- Retention/security tests verify 30-day raw-turn deletion, 180-day structured-trace retention, tenant-scoped raw-text access/audit, and absence of full conversation text or secrets from stdout/events.
- Pipeline E2E tests for success, low confidence, no RAG answer, tool timeout, malformed model JSON, generation failure, automatic retry exhaustion, one repair, fallback, handoff, and full-turn manual retry lineage.
- Concurrency tests for admission saturation, HTTP 429/`Retry-After`, endpoint/profile semaphore limits, waiting-request cancellation, embedding batching, `SKIP LOCKED` worker claims, and idempotent job recovery.
- Failure injection at every node to verify the trace identifies the correct `failure_stage`, component, operation, and field/endpoint.
- Opt-in live-model evaluation isolated from the ordinary suite.
- Conversation-quality evaluation using adapted RULER scenarios plus grounding, citation, route, and handoff measures.

### 15.2 MVP Gates

- High-risk handoff recall is 100% on the curated safety set.
- No golden-set response contains an unsupported price, delivery, contract, refund, action, or handoff claim.
- Intent and tool-route accuracy is at least 95%.
- RAG citation precision is at least 95%.
- Every injected failure is visible at the correct trace node and precise error point.
- Webhook signature, idempotency, retry, and outbox recovery tests pass.
- Manual retry creates an immutable linked trace, enforces authorization/limits, refreshes only live read-only tools, never delivers a second customer reply automatically, and never duplicates a queued/delivered handoff.
- Ordinary tests make no production model, RAG, tool, or Webhook calls.
- No private model profile is marked verified or readiness-healthy until its inventory and capability probes pass against the configured endpoint.
- Cross-customer and cross-tenant identifiers are rejected before downstream access in both real and mock adapter paths.
- The 18-item emotion lock set meets macro-F1 0.80 and has no represented safety-override miss as a provisional regression tripwire; reports include sample support, raw misses, confusion matrix, and confidence intervals and make no generalization claim.
- Latency, capacity-wait time, token counts, and throughput are recorded as a baseline; a later capacity review sets P95 release limits.
- Replacing every example model profile with contract-test fakes requires no application-code change, proving routing is role/capability based.
- No improvement version can activate without checksum-matched passing promotion results and a later human approval event.

## 16. Out of Scope

- Creating or modifying orders, appointments, refunds, accounts, or CRM records.
- Autonomous browser or desktop actions.
- Training or fine-tuning model weights.
- Models larger than the available 35B-A3B role.
- A production customer-facing frontend, SSO, or full contact-center dashboard.
- Automatic long-term customer-profile memory.
- Raw hidden model reasoning capture.
- Autonomous production prompt/policy activation, autonomous application-code changes, or autonomous model-weight training.
- A reusable orchestrator, workflow DSL, dynamic graph editor, plugin runtime, or general-purpose agent SDK.

An orchestrator may be reconsidered after the fixed pipeline is operational only if there are at least two materially different workflows, runtime-configurable graph requirements, or durable pause/resume workflows that cannot be expressed clearly with the existing service and PostgreSQL jobs. It must replace demonstrated duplication or operational pain rather than being introduced for hypothetical flexibility.

## 17. Delivery Sequence

1. Foundation: `uv` project, settings, contracts, flexible Model Registry, mock Model Inventory Gate, PostgreSQL/pgvector migrations, trace/error model, admission/semaphore limits, background jobs, and health checks.
2. Vertical pipeline: fixed `TurnPipeline`, typed nodes, mock RAG/tools, capability-based model router, strategy, validation, fallback, and handoff outbox.
3. Container delivery: production-shaped Dockerfile, Compose services, migrations, worker, frontend, demo seed, health checks, and root README.
4. Console and diagnosis: chat UI, horizontal trace flow, expandable node details, automatic/manual retry visibility, exact error point, health display, and failure injection.
5. Quality: reused scenarios, 60-item labeling workflow, locked baseline evaluation, and live-model opt-in suite.
6. Improvement lifecycle: append-only ledger, manual similarity search, evaluation jobs, mandatory promotion gate, human approval, CLI-based atomic activation, and rollback. Automatic clustering remains deferred.
7. Remote integration: populate `.env`, run the exact-alias Model Inventory Gate before accepting live-model work, validate capabilities/dimensions, replace mocks one adapter at a time, and record performance baseline.
