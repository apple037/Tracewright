# Agent Flow Bootstrap Runtime

## Scope and Reduced-Assurance Warning

Agent Flow is a local/open-model customer-service bootstrap. It provides the
typed turn pipeline, pgvector evidence storage, trace/event APIs, retry lineage,
handoff delivery, retention, and JSON operational logs. Bootstrap validation
uses deterministic gates plus one Qwen semantic judge and therefore reports
`reduced_assurance`. It is not approved for unattended production promotion.

The repository currently exposes a dependency-injected FastAPI shell. A
deployment must supply its authenticator, pipeline, conversation repository,
and trace repository composition. Without that composition `/health/live`
works, while `/health/ready` and authenticated turn APIs correctly remain
unavailable.

## Prerequisites

- Docker Desktop with Compose, or Python 3.12 plus
  [uv](https://docs.astral.sh/uv/).
- A local vLLM OpenAI-compatible endpoint on host port `8000`.
- The exact initial local model `Qwen/Qwen3-8B-AWQ`, configured with
  `max_model_len=6144`.
- Optional remote Ollama-compatible Qwen structured and embedding models.

The PostgreSQL `agent/agent` credentials in Compose are demo bootstrap defaults.
They are unsuitable for production or any network-exposed database.

## Host Development

### `Copy-Item .env.example .env`

```powershell
Copy-Item .env.example .env
```

Edit `.env` locally. It is Git-ignored. Keep model names replaceable in
`config/models.bootstrap.example.yaml`; endpoint URLs and credentials come from
environment variables rather than application code.

### `uv sync --frozen`

```powershell
uv sync --frozen
```

### `Invoke-RestMethod http://localhost:8000/v1/models`

Start vLLM on the Windows host, then verify its inventory:

```powershell
Invoke-RestMethod http://localhost:8000/v1/models |
  ConvertTo-Json -Depth 8
```

The returned model ID must be `Qwen/Qwen3-8B-AWQ`.

### `uv run alembic upgrade head`

Start PostgreSQL/pgvector and apply migrations:

```powershell
uv run --frozen alembic upgrade head
```

### `uv run uvicorn agent_flow.main:app --reload`

```powershell
uv run --frozen uvicorn agent_flow.main:app --reload
```

Liveness is at `http://localhost:8000/health/live`; readiness is at
`http://localhost:8000/health/ready`.

### `uv run python -m agent_flow.worker`

The explicit flag prevents accidental worker startup:

```powershell
uv run --frozen python -m agent_flow.worker --run
```

This single-instance bootstrap runs bounded handoff-outbox and retention loops.
It does not require Redis, RabbitMQ, Kafka, Celery, or an orchestrator.

## Docker Compose

### Why Compose uses host.docker.internal

`localhost` inside `app` or `worker` is the container itself, not host vLLM.
Compose therefore sets:

```text
LOCAL_VLLM_BASE_URL=http://host.docker.internal:8000/v1
extra_hosts=host.docker.internal:host-gateway
```

Both services share the named `agent-net` bridge. PostgreSQL data persists in
`postgres-data`; migration completion gates app, worker, and seed startup.

### `docker compose up --build`

```powershell
docker compose config --quiet
docker compose up --build
```

Runtime `uv run` commands are frozen and exclude dev dependencies, matching the
non-dev image. Shut down with:

```powershell
docker compose down
```

Add `--volumes` only when intentionally deleting the demo database.

### `docker compose --profile demo run --rm demo-seed`

```powershell
docker compose --profile demo run --rm demo-seed
```

The idempotent seed upserts only `tests/fixtures/rag.json` into tenant-scoped
pgvector rows using deterministic offline embeddings. Seeded sources use the
`agent-flow-demo:` namespace and explicit ownership metadata; an existing
non-demo document or chunk collision aborts and rolls back instead of being
overwritten. It validates and counts `tests/fixtures/tools.json`, which remains
file-backed for the mock tool adapter; tool results are deliberately not indexed
as RAG evidence. No model, remote RAG, tool, or webhook endpoint is called.

## Model Registry and Inventory Gate

`config/models.bootstrap.example.yaml` maps roles to replaceable profiles and
profiles to replaceable endpoints. Endpoint semaphores are authoritative when
their cap is lower than the sum of role/profile concurrency.

`response_generator` requires `chat`, `structured_json`, and
`reasoning_toggle`. A matching model name and `max_model_len=6144` are not enough:
the `ResponseDraft` structured-output probe must also pass.

Run the opt-in live local gate only when vLLM is available:

```powershell
uv run --frozen pytest tests/live/test_local_inventory.py -v --run-live-model
```

In the currently observed vLLM combination, exact inventory and the 6144
context check may pass while the strict `ResponseDraft` capability check fails
because `message.content` is null or non-string. Treat that result as not ready;
do not claim the capability gate passed.

## Versioned Prompts, Persona Scope, and Artifact Checksums

Runtime artifacts are exactly:

- `config/prompts/strategy_selector.v1.yaml`
- `config/prompts/response_generator.v1.yaml`
- `config/personas/familiar_companion.zh-TW.v1.yaml`

`familiar_companion.zh-TW` applies only to emotional-support and casual modes,
not transactional responses. Each artifact has a stable ID, semantic version,
schema version, and canonical SHA-256 checksum. Strategy/generation trace events
record those references without copying full prompt/persona bodies. Rollback is
a committed configuration rollback followed by readiness and test verification;
never edit an active checksum in the database.

## Turn, Trace, Event, Health, and Manual Retry APIs

The examples assume the deployment composition has issued valid bearer tokens:

```powershell
$customerHeaders = @{ Authorization = "Bearer <customer-token>" }
$adminHeaders = @{ Authorization = "Bearer <admin-token>" }
$body = @{
  session_id = "demo-session-1"
  message = "我的訂單現在在哪裡？"
} | ConvertTo-Json

$turn = Invoke-RestMethod `
  -Method Post `
  -Uri http://localhost:8080/api/v1/turns `
  -Headers $customerHeaders `
  -ContentType application/json `
  -Body $body

$trace = Invoke-RestMethod `
  -Uri "http://localhost:8080/api/v1/traces/$($turn.trace_id)" `
  -Headers $adminHeaders

$events = Invoke-RestMethod `
  -Uri "http://localhost:8080/api/v1/traces/$($turn.trace_id)/events?after_sequence=0" `
  -Headers $adminHeaders

$retryBody = @{ reason = "operator verified transient dependency recovery" } |
  ConvertTo-Json
$retry = Invoke-RestMethod `
  -Method Post `
  -Uri "http://localhost:8080/api/v1/traces/$($turn.trace_id)/retry" `
  -Headers $adminHeaders `
  -ContentType application/json `
  -Body $retryBody
```

Incremental events use `after_sequence`; retain the highest returned sequence.
Manual retry accepts only a terminal trace, captures a reason, creates a new
immutable trace, preserves `root_trace_id`/`retry_of_trace_id`, caps lineage at
three retries, and marks the result `review_required`.

## Authorization and Tenant Binding

Authentication must convert the bearer token into an
`AuthenticatedPrincipal`. Self-service callers derive `customer_id` from that
principal; a conflicting request customer is rejected before model, RAG, or tool
use. An agent may specify a customer only with `customer:act_as`, within the
same tenant. Session/case ownership and trace access retain the same
tenant/customer binding. Trace read and retry require their internal/admin
scopes; an admin is not implicitly cross-tenant.

## Retry and Handoff Outbox

Retry policy distinguishes safe transient retries from permanent or high-risk
failures. Automatic model/tool retries are bounded. A high-risk or exhausted
path creates an idempotent, tenant-scoped handoff outbox row in the same durable
flow as trace finalization. The worker claims rows with PostgreSQL
`FOR UPDATE SKIP LOCKED`, a claim token, and an expiring lease. HMAC webhook
delivery preserves the idempotency key; terminal failures remain diagnosable.

## Retention and Structured Logs

Raw customer/assistant turn text, turn inputs, and conversation snapshots expire
after 30 days. Structured traces, spans, events, and terminal outbox history
expire after 180 days; active/retryable outbox or retry-lineage references defer
trace deletion.

Stdout is JSON Lines with bounded, secret-filtered fields such as `trace_id`,
`span_id`, `node`, `component`, `operation`, `decision_summary`,
`reason_codes`, `retry`, `tool`, `model`, `error_location`, and `error_code`.
It never emits hidden chain-of-thought, credentials, raw exceptions, or raw
conversation messages. Decision logs contain finite reason codes and summaries,
not model reasoning.

## Test Commands

Ordinary tests are offline and skip opt-in live probes:

```powershell
uv run --frozen pytest -v
```

Explicit PostgreSQL integration requires both the opt-in switch and an isolated
test database URL:

```powershell
$env:REQUIRE_DB_INTEGRATION = "1"
$env:TEST_DATABASE_URL = "postgresql://agent:agent@localhost:5432/agent_test"
uv run --frozen pytest tests/integration -v
Remove-Item Env:REQUIRE_DB_INTEGRATION
Remove-Item Env:TEST_DATABASE_URL
```

The ordinary root suite must not make model, RAG, tool, webhook, or live database
calls. The live model probe is separate:

```powershell
uv run --frozen pytest tests/live/test_local_inventory.py -v --run-live-model
```

On native Windows, psycopg async integration uses a Selector-compatible event
loop. Linux Compose does not require that Windows-only test/runtime adjustment.

## Troubleshooting

- **vLLM inventory:** run
  `Invoke-RestMethod http://localhost:8000/v1/models`; confirm exact model ID
  and inspect the server launch arguments for `--max-model-len 6144`.
- **Structured output:** if `response_format` is rejected, JSON is invalid, or
  `message.content` is null/non-string, the capability gate has failed. Check
  vLLM guided-decoding support and the `ResponseDraft` schema before enabling
  the role.
- **Artifacts/readiness:** call `/health/ready`. `missing` means one of the exact
  prompt/persona files is absent; `invalid` means schema/version/checksum loading
  failed. Do not bypass readiness.
- **Compose host routing:** from the app container, verify
  `http://host.docker.internal:8000/v1/models`; never replace it with container
  localhost. Confirm `extra_hosts` is present in `docker compose config`.
- **Remote Ollama:** query the configured endpoint rather than the CLI's
  implicit localhost:

  ```powershell
  $remoteBase = $env:REMOTE_MODEL_BASE_URL.TrimEnd('/')
  Invoke-RestMethod "$remoteBase/api/tags" | ConvertTo-Json -Depth 8
  foreach ($model in @("qwen3.5:9b", "qwen3:embedding:0.6b")) {
    $body = @{ model = $model } | ConvertTo-Json
    Invoke-RestMethod "$remoteBase/api/show" `
      -Method Post -ContentType application/json -Body $body |
      ConvertTo-Json -Depth 8
  }
  ```

  Private/custom tags must resolve on this same configured server.
- **pgvector:** run
  `docker compose exec postgres psql -U agent -d agent -c "\dx vector"`.
- **Migrations:** run
  `docker compose run --rm migrate` and
  `docker compose exec postgres psql -U agent -d agent -c "select version_num from alembic_version;"`.
- **Semaphore saturation:** compare endpoint `max_concurrency` with the sum of
  role/profile limits. The endpoint cap wins; queued calls are expected.
- **Failed outbox:** inspect only the authorized tenant:
  `select tenant_id,id,status,attempts,last_error_code,last_http_status,next_attempt_at from notification.outbox where tenant_id = '<tenant-id>' and status='failed' order by created_at desc;`.
- **Native Windows psycopg loop:** use the Selector-compatible loop for explicit
  async database integration. This is not needed inside Linux containers.

## Deferred: Incident-first Console, Dual Judge, Improvement Lifecycle

The horizontal incident-first trace console (click-to-expand steps, failed steps
open by default, exact error location, manual retry), bounded context
compaction/per-role views, orchestrator mode and queue expansion, Gemma/Qwen
dual judging, and gated self-improvement ledger are intentionally deferred.
Promotion remains human-only in bootstrap, and any future improvement candidate
must pass deterministic, safety, regression, and semantic tests before atomic
activation.
