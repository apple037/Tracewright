# Private Multi-Model Customer Agent Design

**Date:** 2026-07-20

**Status:** Approved through incremental design review

**Scope:** Read-only customer-service recommendation MVP with high-risk human handoff

## 1. Objective

Build a Python service that accepts multi-turn customer messages, determines the customer's business intent and emotional context, retrieves verified evidence through RAG or read-only tool APIs, chooses a versioned response strategy, generates a response with private remote models, validates the response, and returns either a safe answer or a human-handoff notification.

The service must be diagnosable at node level. Every request must expose a trace that identifies the failed stage, failure reason, precise failure point, retry behavior, and fallback outcome. The system must never store or expose a model's hidden chain-of-thought. It stores structured decision summaries, evidence, reason codes, and model/tool metadata instead.

## 2. Confirmed Constraints

- Backend: Python 3.12, FastAPI, Pydantic v2.
- Package and virtual-environment management: `uv` with a committed `uv.lock`.
- Model service: private remote OpenAI/Ollama-compatible endpoint configured only through `.env`.
- Initial tested model profiles, used as replaceable configuration rather than code-level requirements:
  - `qwen3.6:35b-a3b`: complex strategy and customer-facing generation, with thinking disabled.
  - `qwen3.5:9b`: intent classification, emotion analysis, structured extraction, and response judging.
  - `qwen3:embedding:0.6b`: embeddings.
- Multiple model calls are allowed; larger models than the listed 35B-A3B are not required.
- Business data is not copied into local customer, product, order, or CRM master tables.
- Business facts come only from RAG or read-only tool APIs. MVP integrations use typed mock adapters.
- MVP returns advice only. It cannot create orders, appointments, refunds, or CRM updates.
- High-risk situations are handed to a person through a signed generic Webhook.
- Entry points: REST API and a lightweight browser Test Console.
- Performance is measured as a baseline. The initial release has no hard latency gate.
- Conversation text is retained for 30 days without masking. Structured trace/audit records are retained for 180 days.
- Passwords, API keys, Authorization headers, Cookies, and database connection strings are never logged.

## 3. Architecture Decision

Use a **Deterministic Turn Orchestrator + Typed Nodes + Model Advisors** with explicit state transitions. Do not use a free-running tool-calling agent or LangGraph for the MVP.

Each node has one responsibility, consumes and produces Pydantic models, and records its own span. The orchestrator controls retries, skips, fallbacks, and handoff. Models can classify or generate only inside their assigned node; they cannot choose arbitrary actions or write business data.

### 3.1 Request State Flow

1. `input_gate`: validate input size, identifiers, authentication, and prompt-injection indicators; create `trace_id` and request context.
2. `context_loader`: load the retained conversation window and applicable policy versions.
3. `dialogue_classifier`: use the 9B model to produce business intent, conversation mode, urgency, language, and emotion assessment.
4. `risk_precheck`: apply deterministic high-risk rules before retrieval or generation.
5. `evidence_planner`: determine required RAG collections and read-only tools with typed parameters and freshness requirements.
6. `evidence_collector`: call independent RAG/tool sources concurrently with bounded timeouts and retries.
7. `evidence_validator`: verify source, freshness, required fields, conflicts, and sufficiency. The system does not guess when evidence is insufficient.
8. `strategy_selector`: combine policy, intent, conversation mode, emotion, risk, and evidence into a versioned `StrategyDecision` with reason codes.
9. `response_generator`: use the 35B-A3B model with thinking disabled to generate a response from verified evidence and the selected strategy.
10. `response_validator`: run deterministic format/policy checks and a 9B semantic judge for grounding, citations, tone, route, and risk.
11. `response_repair`: if the draft is repairable, request one constrained rewrite that addresses only listed failures, then validate once more.
12. `finalizer`: return a validated response, a conservative factual fallback, or a safe handoff message.
13. `handoff_notifier`: create an outbox event and deliver a signed Webhook without blocking the safe customer response.

Every transition records `started`, `completed`, `failed`, or `skipped`. Repair is limited to one attempt to prevent loops.

### 3.2 Orchestrator Responsibilities

The Orchestrator owns control flow. It enforces node order, branching, request deadlines, call budgets, cancellation, retry limits, idempotency, fallback, and handoff. It preserves request-scoped state and short-lived strategy state such as “do not probe this direction for the next two turns.” It writes a span for every node and queue wait.

Models are advisors. They return typed classifications, strategy proposals, drafts, or verdicts. They cannot add nodes, reorder the graph, call undeclared tools, execute business actions, override hard risk policies, or retry themselves indefinitely.

Independent RAG and tool calls run concurrently inside `evidence_collector`. Required-source failure cancels unnecessary remaining work. The Orchestrator passes the combined evidence to the next node only after validation.

## 4. Conversation Mode, Emotion, and Strategy

Emotion analysis is an MVP feature but cannot override the customer's actual business request.

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
- `emotion_classifier`;
- `strategy_advisor`;
- `response_generator`;
- `response_judge`;
- `embedding`.

Each role resolves to a named profile in `config/models.yaml`. The versioned YAML contains no secrets. It defines adapter type, model identifier, capabilities, generation parameters, timeout, concurrency, queue capacity, optional fallback profiles, and model-specific request options. `.env` provides endpoint URLs, credentials, configuration path, and optional per-role overrides.

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
    max_concurrency: 4

  quality_generator:
    endpoint: private_chat
    model: qwen3.6:35b-a3b
    capabilities: [chat, reasoning_toggle]
    request_options:
      enable_thinking: false
    temperature: 0.2
    max_concurrency: 2

  semantic_embedding:
    endpoint: private_chat
    model: qwen3:embedding:0.6b
    capabilities: [embedding]
    max_concurrency: 8
    batch_size: 32

roles:
  dialogue_classifier: fast_structured
  emotion_classifier: fast_structured
  strategy_advisor: quality_generator
  response_generator: quality_generator
  response_judge: fast_structured
  embedding: semantic_embedding
```

Users may replace every example model with another small/private model by changing configuration. A replacement is accepted only when its declared capabilities satisfy the role. Fallback chains are explicit; a missing role never silently inherits another profile.

The adapter interface supports OpenAI-compatible chat/embedding endpoints first. Provider-specific details such as Ollama-style thinking controls or extra request bodies live in the adapter/profile, not in the Orchestrator. Logs record the resolved role, profile, model, adapter, and configuration checksum but never credentials.

Startup capability probes verify configured model availability, structured JSON behavior where required, reasoning/thinking disable behavior where declared, and embedding dimension. A capability failure either activates an explicitly configured compatible fallback or makes readiness fail. The service embeds a sentinel string and validates its vector dimension against the migration setting before allowing vector writes.

The initially tested routing uses a smaller structured model for classification/judging, a larger but bounded model for complex strategy/generation, and a dedicated embedding model. Model size is not embedded in routing logic. Deterministic rules remain authoritative for high-risk and executable-action boundaries.

### 5.1 Concurrency, Admission, and Queues

Use three separate concurrency mechanisms:

1. **In-request async fan-out:** `asyncio.TaskGroup` runs independent RAG and read-only tool calls concurrently. Each child has its own timeout, retry policy, cancellation state, and span.
2. **Interactive bounded queues:** the API uses an admission queue plus endpoint/profile concurrency limiters. Generator, classifier, judge, and embedding work do not share a single FIFO queue. Queue saturation returns HTTP 429 with `Retry-After`; it never grows without bound.
3. **PostgreSQL durable jobs:** notification delivery, RAG ingestion/re-embedding, retention, and baseline evaluation run in background workers using `FOR UPDATE SKIP LOCKED`.

Initial safe values are configuration defaults, not performance promises:

```env
APP_MAX_IN_FLIGHT_TURNS=16
APP_TURN_QUEUE_SIZE=32
MODEL_ENDPOINT_MAX_CONCURRENCY=6
GENERATOR_MAX_CONCURRENCY=2
CLASSIFIER_MAX_CONCURRENCY=4
JUDGE_MAX_CONCURRENCY=4
EMBEDDING_MAX_CONCURRENCY=8
EMBEDDING_BATCH_SIZE=32
```

Profile/YAML values can replace these role defaults. Endpoint-wide limits still apply when several profiles share one remote server. Trace data includes `queued_at`, `queue_wait_ms`, `started_at`, execution latency, deadline, and cancellation reason. Disconnects and expired deadlines remove work that has not started.

Do not add Redis, RabbitMQ, Kafka, or Celery for the MVP. Define a `JobQueue` interface so PostgreSQL can be replaced later without changing Orchestrator nodes.

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

## 7. API and Test Console

### 7.1 REST API

- `POST /api/v1/turns`: accepts `session_id`, `customer_id`, optional `case_id`, and `message`; returns `reply`, `trace_id`, citations, conversation mode, and handoff status.
- `GET /api/v1/traces/{trace_id}`: returns the ordered node waterfall with reasons, precise failure points, retries, and fallback results.
- `GET /api/v1/health`: returns liveness/readiness for PostgreSQL, pgvector, model roles, RAG, tools, and notification delivery.
- `GET /console`: serves the lightweight internal Test Console.

The chat API uses a configured bearer token outside development. Trace endpoints require an internal/admin token. The Console reads tokens from server-side configuration and never embeds model or database credentials in client JavaScript.

### 7.2 Console Features

- multi-turn chat and preset customer-service cases;
- response citations and handoff state;
- node waterfall with duration and status;
- expandable model, tool, decision, validation, and notification events;
- error reason and exact error point;
- model configuration and dependency health without secrets;
- emotion-baseline labeling mode.

Use server-rendered static HTML/CSS/JavaScript for the MVP; do not add a frontend framework.

## 8. PostgreSQL and pgvector

Use one PostgreSQL service with separate schemas:

- `rag.documents`: document identity, source, version, checksum, valid dates, access metadata, and ingestion state.
- `rag.chunks`: document reference, ordinal, content, metadata, embedding, and created timestamp. Use cosine distance and an HNSW index.
- `runtime.conversations`: session identity and expiry.
- `runtime.turns`: user/assistant text, citations, trace reference, and expiry. Raw conversation retention is 30 days.
- `observability.traces`: request-level status and timestamps.
- `observability.spans`: node-level parent/child relationships, status, duration, attempts, and failure location.
- `observability.events`: structured node input/output summaries and decision events.
- `observability.model_calls`: role, model, prompt/template version, parameters, token counts, server latency, finish reason, and safe error details.
- `observability.tool_calls`: tool, parameter/result summary, source freshness, HTTP status, retries, and safe error details.
- `notification.outbox`: signed handoff payload, idempotency key, attempts, next-attempt time, and delivery state.
- `runtime.jobs`: durable background work with type, priority, payload, status, available time, attempts, lock owner/time, idempotency key, and last precise error location.

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

Structured logs are emitted as JSON to stdout and rotating files. PostgreSQL is the queryable trace store. An OTLP exporter is optional through environment configuration.

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
- pgvector cosine/HNSW concepts into migrated `rag` schema;
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
- guard against observed 35B long-form essay failures using schema/output limits, deterministic checks, a 9B judge, one repair, and fallback.

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

After all labels are complete, create a stratified 42-item development set and an 18-item locked test set. Prompt and policy iteration use only the development set. The initial target is macro-F1 at least 0.80 on non-ambiguous locked items, with 100% recall for safety overrides in the locked set. Because 60 samples are small, report per-class counts and confidence intervals alongside the score and expand the set over time.

## 13. Testing and Acceptance

### 13.1 Test Layers

- Unit tests for every node, Pydantic contract, strategy rule, risk rule, and error mapping.
- Contract tests shared by mock and remote RAG/tool/model/Webhook adapters.
- PostgreSQL integration tests for migrations, vector dimension, cosine search, HNSW, retention, and outbox delivery.
- Pipeline E2E tests for success, low confidence, no RAG answer, tool timeout, malformed model JSON, generation failure, one repair, fallback, and handoff.
- Concurrency tests for admission saturation, HTTP 429/`Retry-After`, endpoint/profile limits, queue cancellation, embedding batching, `SKIP LOCKED` worker claims, and idempotent job recovery.
- Failure injection at every node to verify the trace identifies the correct `failure_stage`, component, operation, and field/endpoint.
- Opt-in live-model evaluation isolated from the ordinary suite.
- Conversation-quality evaluation using adapted RULER scenarios plus grounding, citation, route, and handoff measures.

### 13.2 MVP Gates

- High-risk handoff recall is 100% on the curated safety set.
- No golden-set response contains an unsupported price, delivery, contract, refund, action, or handoff claim.
- Intent and tool-route accuracy is at least 95%.
- RAG citation precision is at least 95%.
- Every injected failure is visible at the correct trace node and precise error point.
- Webhook signature, idempotency, retry, and outbox recovery tests pass.
- Ordinary tests make no production model, RAG, tool, or Webhook calls.
- Emotion macro-F1 target is at least 0.80 and locked safety-override recall is 100%.
- Latency, queue time, token counts, and throughput are recorded as a baseline; a later capacity review sets P95 release limits.
- Replacing every example Qwen profile with contract-test fakes requires no application-code change, proving routing is role/capability based.

## 14. Out of Scope

- Creating or modifying orders, appointments, refunds, accounts, or CRM records.
- Autonomous browser or desktop actions.
- Training or fine-tuning model weights.
- Models larger than the available 35B-A3B role.
- A production customer-facing frontend, SSO, or full contact-center dashboard.
- Automatic long-term customer-profile memory.
- Raw hidden model reasoning capture.

## 15. Delivery Sequence

1. Foundation: `uv` project, settings, contracts, flexible Model Registry, PostgreSQL/pgvector migrations, trace/error model, queues, and health checks.
2. Vertical pipeline: deterministic Turn Orchestrator, typed nodes, mock RAG/tools, capability-based model router, strategy, validation, fallback, and handoff outbox.
3. Console and diagnosis: chat UI, trace waterfall, exact error point, health display, and failure injection.
4. Quality: reused scenarios, 60-item labeling workflow, locked baseline evaluation, and live-model opt-in suite.
5. Remote integration: populate `.env`, validate model capabilities/dimensions, replace mocks one adapter at a time, and record performance baseline.
