# Incident Trace Console and Inbound Simulator Design

## Goal

Deliver a runnable incident-first console at `/console/` that makes the agent
pipeline, decisions, tool activity, failures, retries, and handoffs easy to
inspect. A secondary chat panel simulates an inbound messaging channel and
executes the real pipeline. The simulator is a development tool; the trace
record is the primary product surface.

The design keeps channel handling outside the pipeline so a future LINE webhook
can use the same application service without duplicating orchestration logic.

## Scope

The first version provides:

- A dark, high-contrast trace and incident console.
- A horizontal pipeline with details expanding downward.
- Failed nodes expanded by default.
- Scoped trace listing, status filtering, attempt selection, and event polling.
- Decision, tool-use, model, artifact, retry, and exact error-location details.
- A collapsible inbound-message simulator that runs the real pipeline.
- Demo bearer-token authentication bound to a tenant, customer, and scopes.
- A persistent PostgreSQL turn queue and one turn-worker instance.
- Real local and remote model calls.
- Contract-compatible, deterministic mock RAG and tool adapters.
- Automatic and manual retries with immutable attempt lineage.
- A channel-neutral application boundary for a future LINE adapter.

The first version does not provide:

- LINE webhook routes, signature verification, or outbound LINE delivery.
- Production JWT/JWKS authentication or production token storage.
- Real customer-data, RAG, or tool APIs.
- Raw model chain-of-thought or unrestricted exception bodies.
- Token-by-token response streaming, WebSockets, a frontend framework, or a
  Node build chain.
- Multiple turn-worker instances.

## Product Positioning

The trace console is the main screen and occupies most of the page. It is an
operator-facing record of what happened and where a problem occurred.

The chat panel is a collapsible simulator. It submits an inbound message, links
the resulting trace, and displays only the final safe reply or handoff. It is
not the intended production customer channel.

All inbound channels converge on one application boundary:

```text
Console simulator --+
Future LINE webhook +--> InboundMessageService --> turn queue --> TurnPipeline
Future channels -----+                                      |
                                                            +--> trace/events
                                                            +--> safe result
```

The future LINE adapter will validate and translate LINE events into the same
`InboundMessage` contract. It will not call pipeline nodes directly.

## Runtime Composition

An explicit runtime composition layer builds the runnable application. It:

- Loads and validates environment settings and the model registry.
- Opens and closes PostgreSQL/pgvector resources in the FastAPI lifespan.
- Creates repositories, the model gateway, and the real `TurnPipeline`.
- Injects deterministic mock RAG and tool adapters.
- Creates the demo bearer-token authenticator.
- Starts dependency and model capability checks.
- Exposes dependency health through `/health/ready`.

`main.py` remains a thin entrypoint that selects `APP_RUNTIME_MODE` and creates
the application. Existing tests may continue to call `create_app(...)` with
injected fakes. Runtime construction does not move into API routes.

The initial mode is `demo`. Unsupported or incomplete modes fail startup with a
safe configuration error rather than silently falling back to the DI shell.

## Demo Authentication

Demo credentials are defined only in `.env` and are excluded from Git. Each
token maps explicitly to:

```text
token -> tenant_id + customer_id + scopes
```

The console asks for the token when it opens. JavaScript keeps it only in page
memory and sends it as an `Authorization: Bearer` header. Refreshing or closing
the page clears it. The token is never placed in URL parameters, localStorage,
sessionStorage, cookies, logs, trace events, or rendered diagnostics.

Self-service submissions cannot choose another `customer_id`; the authenticated
principal supplies it. Administrative trace access requires the appropriate
scope and remains tenant-bound. The runtime displays a persistent `Demo Only`
indicator and refuses demo authentication when configured for a future
production mode.

## Channel-Neutral Message Contract

`InboundMessage` contains:

- Channel and channel-specific external message ID.
- Session and optional case identifiers.
- Authenticated tenant/customer context supplied by the adapter.
- Message text.
- An adapter-generated idempotency key.
- Safe channel metadata from an explicit allowlist.

`OutboundResponse` contains:

- Submission and trace identifiers.
- Completion status.
- Final safe response text and citations, if any.
- Handoff status and safe handoff message, if any.
- Delivery disposition without channel secrets.

Channel adapters own signature verification, identity mapping, external reply
tokens, and delivery. The application service owns authorization, idempotency,
queueing, pipeline execution, and observability.

## Submission Queue and Data Flow

The existing `runtime.jobs` table is activated as the persistent turn queue. No
Redis or additional broker is introduced.

1. The console creates an idempotency key and submits an `InboundMessage` with
   `channel="console"`.
2. `InboundMessageService` validates authorization, message bounds, and the
   idempotency key.
3. One database transaction reserves a trace and enqueues the turn job.
4. The API returns `202 Accepted` with `submission_id`, `trace_id`, and
   `status="queued"`. `submission_id` is the public identifier for the
   underlying job; a second job identifier is not exposed.
5. A single worker claims jobs using `FOR UPDATE SKIP LOCKED`, updates the
   reserved trace to running, and invokes the real pipeline with that trace ID.
6. The console immediately polls trace events after the highest observed
   sequence and updates the horizontal flow.
7. The worker records the bounded safe result and marks the job completed,
   failed, or handed off.
8. The console polls submission status until terminal, then renders the reply
   or handoff and keeps the trace selected.

Enqueue is idempotent within a tenant. Replaying the same key returns the
existing submission and trace without running the pipeline twice. Job request
payloads remain immutable; result and terminal metadata are stored separately.
Expired claims are recoverable. Endpoint and role concurrency controls still
govern model calls even though the first release has one turn worker.

## API Surface

The canonical asynchronous endpoints are:

- `POST /api/v1/submissions`
- `GET /api/v1/submissions/{submission_id}`
- `GET /api/v1/traces`
- `GET /api/v1/traces/{trace_id}`
- `GET /api/v1/traces/{trace_id}/events`
- `POST /api/v1/traces/{trace_id}/retry`

Trace listing is cursor-paginated and may filter by terminal status and time. It
always applies authenticated tenant/customer scope before filters.

The existing `POST /api/v1/turns` remains as a deprecated synchronous facade.
It preserves the current request and terminal `TurnResult` shapes, uses the
same submission service and queue, and waits within a bounded timeout. If the
timeout expires while processing continues, it returns `202 Accepted` with a
`Location` header and the submission reference instead of cancelling the job.
It does not maintain a second direct pipeline path. The asynchronous submission
endpoint is the console and future channel integration contract.

## Model Routing and Capability Checks

Model names and endpoint details live in the model registry, not in pipeline or
frontend code. The initial roles are:

- `dialogue_classifier` and `response_judge`: remote `qwen3.5:9b`.
- `strategy_advisor` and `response_generator`: local
  `Qwen/Qwen3-8B-AWQ`.
- `embedding`: remote `qwen3-embedding-0.6b`.

The local context limit is initially 6144. Users may replace model names,
endpoints, role mappings, capabilities, and concurrency limits through validated
configuration. An endpoint-level concurrency cap always takes precedence over
the sum of role limits.

The remote endpoint uses the OpenAI-compatible adapter. Startup capability
checks verify:

- `/v1/models` is reachable and configured model names resolve.
- Structured roles return valid JSON for the required schemas.
- Structured calls allocate sufficient output tokens and correctly handle
  Qwen reasoning/content separation.
- Embeddings contain exactly 1024 finite values.
- The local generator completes a bounded generation request.

A required capability failure makes `/health/ready` fail with the endpoint,
model role, probe stage, and a safe error code. The runtime does not silently
substitute another model. Capability results never include generated content or
hidden reasoning.

## RAG and Tool Adapters

The first version uses deterministic mock adapters behind the production typed
RAG and tool contracts. They exercise:

- Evidence planning, collection, freshness, and validation.
- Successful tool responses.
- Retryable timeout behavior.
- Permanent failures.
- Insufficient evidence leading to handoff.

The adapters emit the same bounded trace metadata as future real adapters.
Replacing them does not change the pipeline, application service, API, or
frontend.

A canonical tool-request fingerprint prevents repeated identical actions in one
turn. The trace records the duplicate-action decision without exposing
credentials or unrestricted arguments.

## Console Layout

The header contains the product title, `Demo Only` badge, authentication state,
trace filters, auto-refresh control, and last-updated/stale state.

The main workspace contains:

- A trace list with status, channel, time, duration, retry count, and failure
  location.
- An incident strip with failed stage, component, operation, error code, and
  retry disposition.
- A horizontally scrollable pipeline.
- Attempt tabs and immutable retry lineage.
- A collapsible inbound-message simulator.

Each pipeline node is a vertical column:

- A compact status card stays aligned in the horizontal flow.
- Clicking or keyboard-activating the card expands details downward.
- Multiple nodes may remain open.
- Failed nodes open on initial render.
- Running nodes visibly update as new events arrive.

Sending a simulated message automatically selects its new trace. The chat panel
shows the user message, queued/running state, final safe reply or handoff, and a
link back to the selected trace. It does not stream partial response tokens.

## Visual System

The console uses dark gray layered surfaces rather than pure black:

- Page background: near-black blue-gray.
- Node surfaces: lighter charcoal with visible borders.
- Primary text: off-white; secondary text: cool gray.
- Success: green.
- Running/pending: blue.
- Warning/handoff: amber.
- Failure: red.

Focus rings, status icons, labels, and text accompany every color. The flow
remains horizontally scrollable on narrow screens; text never becomes black on
dark surfaces.

## Safe Trace Presentation

Expanded nodes render only bounded structured fields:

- `decision_summary` and reason codes.
- Model role/profile, duration, token usage, and capability metadata.
- Tool name, allowlisted argument metadata, result metadata, and freshness.
- Retry attempt, disposition, and lineage identifiers.
- Error code, failure stage, component, and operation.
- Artifact IDs, semantic versions, and checksums.
- Event sequence and timestamps.

The console never displays Authorization/Cookie/API-key values, database URLs,
raw exception bodies, unrestricted tool arguments, hidden reasoning, or
chain-of-thought. Unknown fields are ignored rather than rendered
automatically. Dynamic values use DOM text nodes or `textContent`; event data is
never assigned to `innerHTML`.

Conversation text follows the existing deliberate retention decision, but
hidden model reasoning is not stored. Operational reasoning is represented by
structured decisions and reason codes.

## Retry and Failure Handling

- Retryable model and adapter failures follow the existing bounded retry policy
  and record every attempt.
- Schema-invalid structured output may enter the repair path.
- A second validation failure produces a handoff.
- High-risk, insufficient-evidence, and safety-validation failures produce a
  safe handoff without speculative output.
- Worker failures retain the last known node, component, operation, safe error
  code, and job attempt.
- Polling failures retain the last successful view, mark it stale, and apply
  bounded backoff.
- Manual retry requires a terminal source trace, a non-empty reason, authorized
  scope, and the configured retry limit.
- A retry creates a new immutable attempt linked to the original root trace.

The console never replaces an errored trace with its retry; both remain
selectable.

## Packaging and Docker Compose

FastAPI serves packaged native HTML, CSS, and JavaScript ES modules at
`/console/`. No Node build is required. Static files are included in the wheel
and Docker image.

Docker Compose runs:

- PostgreSQL with pgvector.
- Database migrations.
- The FastAPI application and console.
- One worker process for turn jobs and existing background duties.

The app reaches the host vLLM endpoint through the configured host-gateway
mapping. Remote endpoints and credentials remain environment-driven. LINE
settings are not required in this milestone.

## Verification

Unit tests cover:

- Demo-token parsing, scope binding, and production-mode rejection.
- Runtime construction and resource cleanup.
- Channel-neutral contract validation.
- Submission idempotency and safe result mapping.
- Configurable model role routing and capability failure reporting.
- Mock RAG/tool success, timeout, permanent failure, and insufficient evidence.

Integration tests cover:

- Atomic trace reservation and job enqueue.
- Concurrent claim behavior using `FOR UPDATE SKIP LOCKED`.
- Duplicate submission replay.
- Expired-claim recovery and bounded retry.
- Tenant/customer isolation for submissions, traces, events, and retries.
- Worker completion, failure, handoff, and result persistence.

End-to-end tests cover:

- A successful real-pipeline turn with fake model transports.
- High-risk handoff.
- Tool timeout and automatic retry.
- Manual retry lineage.
- A failed repair followed by handoff.
- Compatibility `/turns` using the same queue path.

Environment capability tests exercise the configured local and remote servers.
They may be explicitly skipped when those servers are absent, but a running demo
deployment cannot report ready until all configured required probes pass.

Browser tests cover:

- In-memory token gate and logout/refresh behavior.
- Trace listing and scoped filtering.
- Message submission and automatic trace selection.
- Live event polling, horizontal flow, and downward expansion.
- Failed-node default expansion.
- Terminal reply and handoff rendering.
- Manual retry and immutable lineage.
- Keyboard navigation, focus visibility, narrow-screen scrolling, and no
  browser-console errors.

The existing test suite must remain green.

## Completion Criteria

The milestone is complete when:

1. `docker compose up` starts PostgreSQL/pgvector, migrations, the app, and one
   turn worker.
2. `/health/ready` reports database and all required model capabilities ready.
3. A user opens `/console/`, supplies a demo token, and submits a Chinese
   message.
4. The console immediately receives a trace ID and updates pipeline nodes from
   real events.
5. The real pipeline returns a safe model response or explicit handoff.
6. An operator can identify the failed node, component, operation, error code,
   job attempt, and retry lineage from the trace page.
7. No LINE configuration is required, and a future LINE adapter can submit
   through the same `InboundMessageService`.

## Delivery Order

1. Add runtime composition and verified demo authentication.
2. Implement persistent submissions and the single turn worker.
3. Compose the real model gateway, pipeline, and mock RAG/tool adapters.
4. Add trace listing and submission APIs.
5. Package the console shell and token gate.
6. Build the trace-first workspace and live event polling.
7. Add the inbound simulator and terminal reply/handoff display.
8. Add manual retry, lineage, browser verification, Docker verification, and
   full regression testing.
