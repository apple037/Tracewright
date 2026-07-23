# Live Trace Console Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run Agent Flow through a persistent submission queue with real local/remote models, then expose a trace-first console with a collapsible inbound-message simulator.

**Architecture:** A demo runtime composition root injects PostgreSQL repositories, the configured model gateway, deterministic RAG/tool adapters, and demo authentication into FastAPI and one background worker. Channel-neutral submissions reserve a trace and enqueue a PostgreSQL job atomically; the console receives the trace immediately, polls structured events, and displays the terminal safe response or handoff.

**Tech Stack:** Python 3.12, uv, FastAPI, Pydantic 2, psycopg 3, PostgreSQL 16 with pgvector, httpx, pytest, native HTML/CSS/JavaScript ES modules, Docker Compose

## Global Constraints

- Use `uv` for Python dependency and command management.
- Work in the current checkout on local Git; do not create a worktree.
- Do not commit `.env`, `.superpowers/`, `.agents/`, `.claude/`, `__pycache__/`, `*.pyc`, or `.pytest_cache/`.
- The trace console is the primary UI; the chat panel is only an inbound-channel simulator.
- The first release uses one turn-worker instance and PostgreSQL `runtime.jobs`; do not add Redis, Celery, WebSockets, a frontend framework, or a Node build.
- The browser token remains only in JavaScript memory and is never stored in URLs, browser storage, cookies, logs, or traces.
- Bind every self-service request to the tenant and customer in the authenticated principal.
- Do not store or render raw chain-of-thought, reasoning content, credentials, unrestricted tool arguments, or raw exception bodies.
- Model names, endpoints, capabilities, and concurrency limits stay configurable; never hardcode them in pipeline or frontend code.
- Initial routing is remote `qwen3.5:9b` for structured roles, local `Qwen/Qwen3-8B-AWQ` for strategy/generation, and remote `qwen3-embedding-0.6b` for embeddings.
- The local vLLM deployment uses a 6144-token model context limit.
- An endpoint concurrency cap always wins over the sum of role limits.
- RAG and tool data use deterministic mock adapters behind the existing typed contracts.
- Keep the existing 30-day conversation retention decision and never persist hidden model reasoning.
- Use TDD for every behavior change and commit after every task.

## File Structure

New backend units:

- `src/agent_flow/inbound.py`: channel-neutral commands, submission orchestration, and bounded legacy waiting.
- `src/agent_flow/repositories/submissions.py`: atomic trace/job enqueue, scoped reads, claims, leases, results, and recovery.
- `src/agent_flow/turn_worker.py`: executes claimed turn jobs and settles them.
- `src/agent_flow/runtime.py`: demo composition root shared by app and worker.
- `src/agent_flow/api/submissions.py`: asynchronous submission HTTP contract.
- `migrations/versions/0003_turn_submissions.py`: queued trace state and job execution metadata.
- `config/demo/rag.json`, `config/demo/tools.json`: packaged deterministic demo evidence.

New frontend units:

- `src/agent_flow/console/index.html`: accessible trace-first page structure.
- `src/agent_flow/console/styles.css`: dark horizontal-flow layout.
- `src/agent_flow/console/api.js`: authenticated same-origin API client.
- `src/agent_flow/console/state.js`: token, trace, event cursor, expansion, lineage, and simulator state.
- `src/agent_flow/console/render.js`: allowlisted safe DOM rendering.
- `src/agent_flow/console/app.js`: login, polling, submission, selection, and retry orchestration.

Keep `main.py`, API route files, and `worker.py` thin. Do not put SQL in API routes or runtime construction in pipeline nodes.

---

### Task 1: Align model configuration and capability probes

**Files:**
- Modify: `config/models.bootstrap.example.yaml`
- Modify: `.env.example`
- Modify: `src/agent_flow/model_registry.py`
- Modify: `src/agent_flow/adapters/models.py`
- Modify: `tests/contract/test_model_inventory.py`
- Modify: `tests/live/test_local_inventory.py`
- Modify: `tests/e2e/test_compose_config.py`

**Interfaces:**
- Consumes: `Settings`, `ModelRegistry.resolve(role)`, `ModelGateway.structured(...)`, `EmbeddingModel.embed(...)`
- Produces: `ModelInventoryProbe.probe_role(role) -> InventoryResult` with exact structured and embedding validation for the configured OpenAI-compatible endpoints

- [ ] **Step 1: Write failing configuration and probe tests**

Add assertions that the bootstrap registry resolves the remote endpoint as
OpenAI-compatible, resolves the exact three initial model names, and rejects an
embedding vector whose dimension is not 1024 or contains non-finite values:

```python
def test_bootstrap_routes_initial_demo_models(model_config, settings):
    registry = ModelRegistry(model_config, settings)
    assert registry.resolve("dialogue_classifier").model == "qwen3.5:9b"
    assert registry.resolve("dialogue_classifier").adapter == "openai_compatible"
    assert registry.resolve("response_generator").model == "Qwen/Qwen3-8B-AWQ"
    assert registry.resolve("response_generator").min_context_length == 6144
    assert registry.resolve("embedding").model == "qwen3-embedding-0.6b"


@pytest.mark.parametrize("vector", [[0.0] * 1023, [float("nan")] * 1024])
async def test_embedding_probe_rejects_invalid_shape_or_values(
    registry, respx_mock, vector
):
    stub_openai_inventory(respx_mock, "qwen3-embedding-0.6b")
    respx_mock.post("/v1/embeddings").mock(
        return_value=httpx.Response(200, json={"data": [{"embedding": vector}]})
    )
    with pytest.raises(RuntimeError, match="embedding.*1024 finite"):
        await ModelInventoryProbe(registry).probe_role("embedding")
```

Add a structured-response test where `finish_reason="length"` and assert the
probe reports role and capability stage without including response content.

- [ ] **Step 2: Run the focused tests and verify failure**

Run:

```powershell
uv run pytest tests/contract/test_model_inventory.py tests/e2e/test_compose_config.py -q
```

Expected: FAIL because the remote adapter/tag and strict embedding checks do not
yet match the approved design.

- [ ] **Step 3: Update model routing and bounded probe validation**

Set the remote endpoint adapter to `openai_compatible`, the embedding model to
`qwen3-embedding-0.6b`, and the remote structured profile `max_tokens` to 2048
with `enable_thinking: false`. Keep endpoint max concurrency at 6 and ensure
role limits do not claim more effective concurrency than that endpoint.

Add `min_context_length: int | None = Field(default=None, ge=1)` to
`ProfileConfig` and `ResolvedModel`; configure it as 6144 on
`local_generator`. Inventory probing fails with a safe capability error when
the local `/v1/models` entry omits `max_model_len` or reports less than 6144.

Validate the structured finish reason and embedding values:

```python
def _validate_embedding_vector(vector: list[float]) -> None:
    if len(vector) != 1024 or not all(math.isfinite(value) for value in vector):
        raise RuntimeError("embedding capability requires exactly 1024 finite values")


_, response = await gateway.structured_response(role, request, ResponseDraft)
if response.finish_reason == "length":
    raise RuntimeError(
        f"capability probe failed for role {role} at capability: output truncated"
    )
```

Keep response content and reasoning fields out of every exception. If
`structured_response` is introduced, define it as:

```python
async def structured_response(
    self, role: str, request: object, response_type: type[T]
) -> tuple[T, ModelResponse]:
    resolved = self.registry.resolve(role)
    required = {"chat", "structured_json"}
    if not required <= resolved.capabilities:
        raise RuntimeError(
            f"role {role} lacks required capabilities: chat, structured_json"
        )
    schema = response_type.model_json_schema()
    if resolved.adapter == "openai_compatible":
        response = await self._openai_chat(
            resolved,
            request,
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": response_type.__name__,
                    "strict": True,
                    "schema": schema,
                },
            },
        )
    elif resolved.adapter == "ollama_compatible":
        content = await self._ollama_chat(
            resolved, request, response_format=schema
        )
        response = ModelResponse(text=content, finish_reason="stop")
    else:
        raise RuntimeError(f"unsupported model adapter: {resolved.adapter}")
    return response_type.model_validate_json(response.text), response
```

and keep `structured(...) -> T` as the compatibility wrapper used by pipeline
nodes.

Probe each configured structured role with its actual output contract:

```python
ROLE_PROBE_SCHEMAS = {
    "dialogue_classifier": DialogueClassificationResult,
    "strategy_advisor": StrategyProposalResult,
    "response_generator": ResponseDraft,
    "response_judge": JudgeVerdictResult,
    "response_judge_zh_verifier": JudgeVerdictResult,
    "promotion_judge_primary": JudgeVerdictResult,
    "promotion_judge_secondary": JudgeVerdictResult,
}
```

Disabled roles are skipped. Do not use `ResponseDraft` as the universal probe
schema because that can pass while a classifier or judge contract fails.

- [ ] **Step 4: Run focused and live-safe tests**

Run:

```powershell
uv run pytest tests/contract/test_model_inventory.py tests/live/test_local_inventory.py tests/e2e/test_compose_config.py -q
```

Expected: all environment-independent tests PASS; live tests SKIP explicitly
when their opt-in environment flag is absent.

- [ ] **Step 5: Commit**

```powershell
git add config/models.bootstrap.example.yaml .env.example src/agent_flow/model_registry.py src/agent_flow/adapters/models.py tests/contract/test_model_inventory.py tests/live/test_local_inventory.py tests/e2e/test_compose_config.py
git commit -m "fix: align demo model capabilities"
```

---

### Task 2: Add channel-neutral submission contracts and schema

**Files:**
- Modify: `src/agent_flow/contracts.py`
- Create: `migrations/versions/0003_turn_submissions.py`
- Modify: `tests/unit/test_contracts.py`
- Modify: `tests/integration/test_migration_upgrade.py`

**Interfaces:**
- Produces: `InboundMessage`, `SubmissionReceipt`, `SubmissionResult`, and `SubmissionStatus`
- Produces: queued traces with channel identifiers and job columns `trace_id`, `result`, `finished_at`, `lease_expires_at`, and `claim_token`
- Consumes: existing `TurnRequest`, `TurnResult`, and `HandoffEvent`

- [ ] **Step 1: Write failing contract tests**

```python
def test_inbound_message_forbids_identity_override_and_unknown_metadata():
    message = InboundMessage(
        channel="console",
        external_message_id="console-01",
        session_id="session-01",
        text="我的訂單在哪裡？",
        idempotency_key="console-01",
        metadata={"source": "trace-console"},
    )
    assert message.to_turn_request() == TurnRequest(
        session_id="session-01", message="我的訂單在哪裡？"
    )
    assert not hasattr(message, "customer_id")


def test_submission_result_contains_only_safe_terminal_fields():
    result = SubmissionResult.model_validate(
        {
            "submission_id": str(uuid4()),
            "trace_id": str(uuid4()),
            "status": "completed",
            "text": "已完成",
            "citations": [],
            "handoff": None,
        }
    )
    assert "reasoning" not in result.model_dump()
```

Use `Literal["console", "line"]` only if it does not force implementation of a
LINE adapter. Prefer a bounded channel string so future adapters do not require
a migration.

- [ ] **Step 2: Run contract tests and verify failure**

Run:

```powershell
uv run pytest tests/unit/test_contracts.py -q
```

Expected: FAIL because the submission contracts do not exist.

- [ ] **Step 3: Implement strict contracts**

Add:

```python
SubmissionStatus = Literal["queued", "running", "completed", "failed"]


class InboundMessage(StrictModel):
    channel: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,31}$")
    external_message_id: str = Field(min_length=1, max_length=256)
    session_id: str = Field(min_length=1, max_length=256)
    text: str = Field(min_length=1, max_length=20_000)
    case_id: str | None = Field(default=None, max_length=256)
    idempotency_key: str = Field(min_length=1, max_length=256)
    metadata: dict[str, str] = Field(default_factory=dict)

    def to_turn_request(self) -> TurnRequest:
        return TurnRequest(
            session_id=self.session_id, message=self.text, case_id=self.case_id
        )


class SubmissionReceipt(StrictModel):
    submission_id: UUID
    trace_id: UUID
    status: SubmissionStatus


class SubmissionResult(SubmissionReceipt):
    text: str | None = None
    citations: tuple[str, ...] = ()
    handoff: HandoffEvent | None = None
    error_code: str | None = None
    error_component: str | None = None
```

Validate metadata keys against `{"source", "locale"}` and reject all other
keys.

- [ ] **Step 4: Write and verify the migration**

Create revision `0003_turn_submissions` after `0002_handoff_outbox`. It must:

```python
op.drop_constraint("ck_traces_status", "traces", schema="observability")
op.create_check_constraint(
    "ck_traces_status",
    "traces",
    "status IN ('queued', 'running', 'succeeded', 'failed')",
    schema="observability",
)
op.add_column(
    "traces",
    sa.Column("channel", sa.Text(), nullable=True),
    schema="observability",
)
op.add_column(
    "traces",
    sa.Column("external_message_id", sa.Text(), nullable=True),
    schema="observability",
)
op.add_column(
    "jobs",
    sa.Column(
        "trace_id",
        UUID,
        sa.ForeignKey("observability.traces.id", ondelete="CASCADE"),
        nullable=True,
    ),
    schema="runtime",
)
op.add_column("jobs", sa.Column("result", JSONB), schema="runtime")
op.add_column(
    "jobs", sa.Column("finished_at", sa.DateTime(timezone=True)), schema="runtime"
)
op.add_column(
    "jobs", sa.Column("lease_expires_at", sa.DateTime(timezone=True)), schema="runtime"
)
op.add_column("jobs", sa.Column("claim_token", UUID), schema="runtime")
op.create_index("ix_jobs_trace", "jobs", ["trace_id"], schema="runtime")
```

Make upgrade idempotently converge from supported partial bootstrap states in
the same style as `0002_handoff_outbox.py`. Add migration assertions for every
column, foreign key, index, and status constraint. Existing traces may retain
null channel fields; every queued submission must set both fields. Extend
`TraceRecord` and trace row selection in Task 3 with `channel` and
`external_message_id`; Task 6 verifies trace list responses expose them.

Run:

```powershell
uv run pytest tests/integration/test_migration_upgrade.py -q
```

Expected: PASS when the integration database is available, otherwise the
existing explicit skip behavior remains.

- [ ] **Step 5: Commit**

```powershell
git add src/agent_flow/contracts.py migrations/versions/0003_turn_submissions.py tests/unit/test_contracts.py tests/integration/test_migration_upgrade.py
git commit -m "feat: define queued turn submissions"
```

---

### Task 3: Implement the persistent submission repository

**Files:**
- Create: `src/agent_flow/repositories/submissions.py`
- Modify: `src/agent_flow/repositories/traces.py`
- Create: `tests/integration/test_submission_repository.py`
- Modify: `tests/e2e/conftest.py`

**Interfaces:**
- Produces: `PostgresSubmissionRepository.enqueue(...) -> SubmissionRecord`
- Produces: `get(...)`, `claim(...)`, `heartbeat(...)`, `complete(...)`, `fail(...)`, and `recover_expired_claim(...)`
- Produces: `PostgresTraceRepository.activate_trace(trace_id, tenant_id)`
- Consumes: `AuthorizedCustomerContext`, `InboundMessage`, `TurnResult`, and `PostgresPool`

- [ ] **Step 1: Write failing atomic enqueue and scope tests**

```python
async def test_enqueue_atomically_reserves_trace_and_deduplicates(
    submissions, customer_context
):
    message = inbound_message(idempotency_key="same-key")
    first = await submissions.enqueue(customer_context, message)
    second = await submissions.enqueue(customer_context, message)
    assert second == first
    assert first.status == "queued"
    assert await trace_status(first.trace_id) == "queued"
    assert await job_count("same-key", tenant_id="t1") == 1


async def test_same_key_in_another_tenant_is_distinct(
    submissions, customer_context, other_tenant_context
):
    first = await submissions.enqueue(
        customer_context, inbound_message(idempotency_key="shared")
    )
    second = await submissions.enqueue(
        other_tenant_context, inbound_message(idempotency_key="shared")
    )
    assert first.id != second.id


async def test_scoped_get_hides_other_customer(submissions, customer_context):
    record = await submissions.enqueue(customer_context, inbound_message())
    assert await submissions.get(
        record.id, tenant_id="t1", customer_id="c2"
    ) is None
```

- [ ] **Step 2: Run the repository test and verify failure**

Run:

```powershell
uv run pytest tests/integration/test_submission_repository.py -q
```

Expected: FAIL because `PostgresSubmissionRepository` does not exist.

- [ ] **Step 3: Implement immutable records and atomic enqueue**

Define:

```python
@dataclass(frozen=True)
class SubmissionRecord:
    id: UUID
    trace_id: UUID
    tenant_id: str
    customer_id: str
    status: str
    attempts: int
    payload: dict[str, Any]
    result: dict[str, Any] | None
    last_error_code: str | None
    last_error_component: str | None
    lease_expires_at: datetime | None
    claim_token: UUID | None
    created_at: datetime
    finished_at: datetime | None

    def to_result(self) -> SubmissionResult:
        if self.result is not None:
            return SubmissionResult.model_validate(self.result)
        return SubmissionResult(
            submission_id=self.id,
            trace_id=self.trace_id,
            status=self.status,
            error_code=self.last_error_code,
            error_component=self.last_error_component,
        )


async def enqueue(
    self,
    context: AuthorizedCustomerContext,
    message: InboundMessage,
    *,
    retry_of_trace_id: UUID | None = None,
    retry_initiator: str | None = None,
    retry_reason: str | None = None,
    delivery_disposition: str | None = None,
) -> SubmissionRecord:
    submission_id, trace_id = uuid4(), uuid4()
    payload = {
        "message": message.model_dump(mode="json"),
        "retry": {
            "retry_of_trace_id": (
                str(retry_of_trace_id) if retry_of_trace_id is not None else None
            ),
            "retry_initiator": retry_initiator,
            "retry_reason": retry_reason,
            "delivery_disposition": delivery_disposition,
        },
    }
    async with self._pool.connection() as connection:
        async with connection.transaction():
            existing = await self._get_by_key(
                connection, context.tenant_id, message.idempotency_key
            )
            if existing is not None:
                if (
                    existing.customer_id != context.customer_id
                    or existing.payload != payload
                ):
                    raise ValueError("submission idempotency conflict")
                return existing
            root_id, retry_sequence = await self._resolve_lineage(
                connection,
                trace_id=trace_id,
                retry_of_trace_id=retry_of_trace_id,
                context=context,
            )
            await self._insert_queued_trace(
                connection,
                trace_id=trace_id,
                root_trace_id=root_id,
                retry_of_trace_id=retry_of_trace_id,
                retry_sequence=retry_sequence,
                retry_initiator=retry_initiator,
                retry_reason=retry_reason,
                delivery_disposition=delivery_disposition,
                context=context,
                session_id=message.session_id,
            )
            await self._insert_job(
                connection,
                submission_id=submission_id,
                trace_id=trace_id,
                context=context,
                payload=payload,
                idempotency_key=message.idempotency_key,
            )
            return await self._get_required(connection, submission_id)
```

Within one transaction:

1. Lock or read the tenant/idempotency-key job.
2. Return the existing immutable record when payload and scope match.
3. Reject the replay with `ValueError("submission idempotency conflict")` when
   the payload differs.
4. Resolve retry root and next sequence under lock when retrying.
5. Insert an `observability.traces` row with `status='queued'`.
6. Insert a `runtime.jobs` row with `job_type='turn'`, the trace ID, and only
   `InboundMessage.model_dump(mode="json")` plus retry metadata.

Set the trace `channel` and `external_message_id` from the validated message.
Extend `TraceRecord`, `_COLUMNS`, and every trace row mapper so legacy rows
return null for those fields and submitted rows return their exact values.

Do not put bearer tokens or principals in the payload.

- [ ] **Step 4: Write failing claim, heartbeat, and settlement tests**

Cover:

```python
async def test_claim_uses_unique_token_and_skip_locked(
    submissions, customer_context
):
    queued = await submissions.enqueue(customer_context, inbound_message())
    first = await submissions.claim(
        owner="worker-a", limit=1, lease_seconds=30, max_attempts=3
    )
    second = await submissions.claim(
        owner="worker-b", limit=1, lease_seconds=30, max_attempts=3
    )
    assert [item.id for item in first] == [queued.id]
    assert first[0].claim_token is not None
    assert second == ()


async def test_heartbeat_requires_matching_owner_and_claim_token(
    submissions, customer_context
):
    await submissions.enqueue(customer_context, inbound_message())
    claimed = (
        await submissions.claim(
            owner="worker-a", limit=1, lease_seconds=30, max_attempts=3
        )
    )[0]
    assert not await submissions.heartbeat(
        claimed.id, owner="worker-b",
        claim_token=claimed.claim_token, lease_seconds=30
    )
    assert await submissions.heartbeat(
        claimed.id, owner="worker-a",
        claim_token=claimed.claim_token, lease_seconds=30
    )


async def test_complete_is_idempotent_for_identical_safe_result(
    submissions, customer_context
):
    await submissions.enqueue(customer_context, inbound_message())
    claimed = (
        await submissions.claim(
            owner="worker-a", limit=1, lease_seconds=30, max_attempts=3
        )
    )[0]
    result = completed_submission_result(claimed)
    assert await submissions.complete(
        claimed.id, owner="worker-a",
        claim_token=claimed.claim_token, result=result
    )
    assert await submissions.complete(
        claimed.id, owner="worker-a",
        claim_token=claimed.claim_token, result=result
    )


async def test_fail_stores_only_error_code_and_component(
    submissions, customer_context
):
    await submissions.enqueue(customer_context, inbound_message())
    claimed = (
        await submissions.claim(
            owner="worker-a", limit=1, lease_seconds=30, max_attempts=3
        )
    )[0]
    await submissions.fail(
        claimed.id, owner="worker-a", claim_token=claimed.claim_token,
        error_code="MODEL_TIMEOUT", error_component="response_generator",
        retryable=False, max_attempts=3, backoff_seconds=1,
    )
    stored = await submissions.get(
        claimed.id, tenant_id=claimed.tenant_id,
        customer_id=claimed.customer_id,
    )
    assert stored.last_error_code == "MODEL_TIMEOUT"
    assert stored.last_error_component == "response_generator"
    assert "exception" not in json.dumps(stored.result or {})


async def test_expired_running_claim_is_recoverable(
    submissions, customer_context, expire_job_lease
):
    await submissions.enqueue(customer_context, inbound_message())
    first = (
        await submissions.claim(
            owner="worker-a", limit=1, lease_seconds=30, max_attempts=3
        )
    )[0]
    await expire_job_lease(first.id)
    recovered = await submissions.claim(
        owner="worker-b", limit=1, lease_seconds=30, max_attempts=3
    )
    assert recovered[0].id == first.id
    assert recovered[0].attempts == 2
    assert recovered[0].claim_token != first.claim_token
```

Use two repository instances to prove a locked job is not double-claimed.

- [ ] **Step 5: Implement lease-safe claim and settlement**

Use a single transaction with:

```sql
SELECT id
FROM runtime.jobs
WHERE job_type = 'turn'
  AND (
    (status = 'queued' AND available_at <= now())
    OR (status = 'running' AND lease_expires_at <= now())
  )
ORDER BY priority, created_at
FOR UPDATE SKIP LOCKED
LIMIT %s
```

Set `status='running'`, increment attempts, and assign a fresh `claim_token`.
Every heartbeat/complete/fail update must match `id`, `lock_owner`,
`claim_token`, and `status='running'`. `complete` stores only
`SubmissionResult.model_dump(mode="json")`. `fail` stores bounded error code
and component, not `str(exception)`.

Add an atomic recovery method:

```python
async def recover_expired_claim(
    self,
    submission_id: UUID,
    *,
    owner: str,
    claim_token: UUID,
    error_code: str = "WORKER_LEASE_EXPIRED",
) -> SubmissionRecord:
```

It locks the claimed job and current trace. If the trace is still queued, it
returns the same record. If running, it finalizes that trace as failed with one
safe recovery event, inserts a queued retry trace under the same root, updates
`runtime.jobs.trace_id`, and returns the replacement record. It never changes
the public submission ID or immutable message payload.

Add:

```python
async def activate_trace(
    self,
    trace_id: UUID,
    *,
    tenant_id: str,
    expected_retry_of: UUID | None,
) -> None:
```

It is a compare-and-set from queued to running and verifies the stored
`retry_of_trace_id IS NOT DISTINCT FROM expected_retry_of`. Repeating the same
activation for an already-running trace is idempotent; terminal traces,
cross-tenant IDs, and lineage mismatches are rejected.

- [ ] **Step 6: Run integration tests**

Run:

```powershell
uv run pytest tests/integration/test_submission_repository.py tests/integration/test_trace_repository.py -q
```

Expected: PASS or the repository suite's documented database skip.

- [ ] **Step 7: Commit**

```powershell
git add src/agent_flow/repositories/submissions.py src/agent_flow/repositories/traces.py tests/integration/test_submission_repository.py tests/e2e/conftest.py
git commit -m "feat: persist turn submission queue"
```

---

### Task 4: Execute reserved traces through a turn worker

**Files:**
- Modify: `src/agent_flow/pipeline/turn.py`
- Create: `src/agent_flow/turn_worker.py`
- Modify: `src/agent_flow/worker.py`
- Create: `tests/unit/test_turn_worker.py`
- Modify: `tests/unit/pipeline/test_turn.py`
- Modify: `tests/e2e/conftest.py`

**Interfaces:**
- Produces: `TurnPipeline.run(..., trace_id: UUID | None = None) -> TurnResult`
- Produces: `TurnJobWorker.run_once() -> int` and `TurnJobWorker.run(stop)`
- Consumes: `PostgresSubmissionRepository`, `TurnPipeline`, `InboundMessage`, and `AuthorizedCustomerContext`

- [ ] **Step 1: Write a failing reserved-trace pipeline test**

```python
async def test_pipeline_activates_reserved_trace_without_creating_another(
    pipeline, context, fake_models
):
    trace_id = await pipeline.traces.reserve_for_test(
        tenant_id=context.tenant_id,
        customer_id=context.customer_id,
        session_id="s1",
    )
    result = await pipeline.run(
        context,
        TurnRequest(session_id="s1", message="查詢訂單"),
        trace_id=trace_id,
    )
    assert result.trace_id == trace_id
    assert len(pipeline.traces.records) == 1
```

- [ ] **Step 2: Run the test and verify failure**

Run:

```powershell
uv run pytest tests/unit/pipeline/test_turn.py -q
```

Expected: FAIL because `TurnPipeline.run` cannot accept a reserved trace.

- [ ] **Step 3: Add reserved-trace activation**

Change the signature to:

```python
async def run(
    self,
    context: AuthorizedCustomerContext,
    request: TurnRequest,
    retry_of: UUID | None = None,
    *,
    trace_id: UUID | None = None,
    retry_initiator: str | None = None,
    retry_reason: str | None = None,
    delivery_disposition: str | None = None,
    suppress_handoff: bool = False,
    max_retry_count: int | None = None,
) -> TurnResult:
```

When `trace_id is None`, preserve the existing `start_trace` path. When it is
provided, call
`traces.activate_trace(trace_id, tenant_id, expected_retry_of=retry_of)` and run
all existing nodes against that ID. For queued manual retries, the worker passes
the stored `retry_of`; activation verifies it matches the immutable reserved
lineage before the existing retry artifact/snapshot path runs.

- [ ] **Step 4: Write failing worker tests**

```python
async def test_worker_completes_submission_with_safe_turn_result(
    queued_submission, fake_submission_repository, pipeline
):
    worker = TurnJobWorker(
        repository=fake_submission_repository,
        pipeline=pipeline,
        owner="worker-1",
        lease_seconds=60,
    )
    assert await worker.run_once() == 1
    settled = await fake_submission_repository.get_unscoped(
        queued_submission.id
    )
    assert settled.status == "completed"
    assert settled.result["trace_id"] == str(queued_submission.trace_id)
    assert "reasoning" not in settled.result


async def test_worker_failure_records_location_without_exception_body(
    fake_submission_repository, failing_pipeline
):
    worker = TurnJobWorker(
        repository=fake_submission_repository,
        pipeline=failing_pipeline,
        owner="worker-1",
    )
    await worker.run_once()
    stored = fake_submission_repository.records[0]
    assert stored.last_error_code == "MODEL_TIMEOUT"
    assert stored.last_error_component == "response_generator"
    assert "private transport body" not in json.dumps(stored.result or {})


async def test_worker_heartbeats_while_pipeline_is_running(
    fake_submission_repository, blocking_pipeline
):
    worker = TurnJobWorker(
        repository=fake_submission_repository,
        pipeline=blocking_pipeline,
        owner="worker-1",
        lease_seconds=2,
    )
    task = asyncio.create_task(worker.run_once())
    await blocking_pipeline.started.wait()
    await asyncio.sleep(1.1)
    assert fake_submission_repository.heartbeat_calls >= 1
    blocking_pipeline.release.set()
    assert await task == 1


async def test_reclaimed_active_trace_creates_retry_lineage(
    fake_submission_repository, pipeline
):
    fake_submission_repository.records[0].attempts = 2
    fake_submission_repository.trace_status = "running"
    worker = TurnJobWorker(
        repository=fake_submission_repository,
        pipeline=pipeline,
        owner="worker-2",
    )
    await worker.run_once()
    assert (
        fake_submission_repository.abandoned_error_code
        == "WORKER_LEASE_EXPIRED"
    )
    assert (
        fake_submission_repository.records[0].trace_id
        != fake_submission_repository.original_trace_id
    )
    assert (
        fake_submission_repository.retry_of_trace_id
        == fake_submission_repository.original_trace_id
    )
```

- [ ] **Step 5: Implement the turn worker**

`TurnJobWorker` must:

```python
class TurnJobWorker:
    async def run_once(self) -> int:
        claimed = await self.repository.claim(
            owner=self.owner,
            limit=self.batch_size,
            lease_seconds=self.lease_seconds,
            max_attempts=self.max_attempts,
        )
        async with asyncio.TaskGroup() as group:
            for record in claimed:
                group.create_task(self._execute(record))
        return len(claimed)
```

For each record, validate `InboundMessage` from immutable payload, reconstruct
`AuthorizedCustomerContext` only from stored tenant/customer scope, heartbeat
at less than half the lease interval, call the pipeline with the reserved trace,
and settle with `SubmissionResult`. Pass the stored `delivery_disposition` and
retry suppression metadata into `TurnPipeline.run`; never trust channel
metadata to override them.

If a process died after activating a trace, recovery must finalize the abandoned
trace with safe code `WORKER_LEASE_EXPIRED`, reserve a retry trace under the same
root, update the job's current trace ID atomically, and execute the new trace.
The original and recovery traces remain queryable.

Classify repository/network timeouts as retryable. Treat invalid payloads,
authorization-scope corruption, and exhausted attempts as terminal. Never put
an exception string in job result or trace payload.

- [ ] **Step 6: Add the worker loop without duplicating runtime construction**

Extend `run_worker_runtime(...)` to accept a constructed `TurnJobWorker` and
run it beside the outbox and retention loops. Keep `worker.py` responsible only
for process lifecycle; Task 7 will supply the production composition.

- [ ] **Step 7: Run worker and pipeline tests**

Run:

```powershell
uv run pytest tests/unit/test_turn_worker.py tests/unit/pipeline/test_turn.py tests/e2e/test_turn_pipeline.py tests/e2e/test_failure_locations.py -q
```

Expected: PASS.

- [ ] **Step 8: Commit**

```powershell
git add src/agent_flow/pipeline/turn.py src/agent_flow/turn_worker.py src/agent_flow/worker.py tests/unit/test_turn_worker.py tests/unit/pipeline/test_turn.py tests/e2e/conftest.py
git commit -m "feat: execute queued turns"
```

---

### Task 5: Record safe model, decision, RAG, and tool telemetry

**Files:**
- Create: `src/agent_flow/observability.py`
- Modify: `src/agent_flow/adapters/models.py`
- Modify: `src/agent_flow/adapters/evidence.py`
- Modify: `src/agent_flow/pipeline/turn.py`
- Create: `tests/unit/test_operation_telemetry.py`
- Modify: `tests/unit/pipeline/test_turn.py`
- Modify: `tests/e2e/test_turn_pipeline.py`

**Interfaces:**
- Produces: `OperationTelemetry.bind_node(...)`, `record_model(...)`, `record_rag(...)`, and `record_tool(...)`
- Produces: `summarize_node_result(node, result) -> dict[str, JSONValue]`
- Consumes: the active trace/span context, model responses, typed node results, and trace repository

- [ ] **Step 1: Write failing telemetry safety tests**

```python
async def test_model_telemetry_records_profile_tokens_and_duration_only(
    telemetry, trace_context
):
    async with telemetry.bind_node(trace_context):
        await telemetry.record_model(
            role="response_generator",
            profile="local_generator",
            model="Qwen/Qwen3-8B-AWQ",
            duration_ms=42,
            input_tokens=120,
            output_tokens=36,
            finish_reason="stop",
            status="completed",
        )
    event = telemetry.traces.events[-1]
    assert event.event_type == "model_call"
    assert event.payload["model_role"] == "response_generator"
    assert event.payload["duration_ms"] == 42
    assert "text" not in event.payload
    assert "reasoning" not in json.dumps(event.payload)


async def test_tool_telemetry_hashes_arguments_without_storing_values(
    telemetry, trace_context
):
    async with telemetry.bind_node(trace_context):
        await telemetry.record_tool(
            tool="order.lookup",
            arguments={"order_id": "private-order-1"},
            duration_ms=12,
            status="completed",
            freshness_seconds=60,
        )
    payload = telemetry.traces.events[-1].payload
    assert payload["tool"] == "order.lookup"
    assert len(payload["argument_fingerprint"]) == 64
    assert "private-order-1" not in json.dumps(payload)


def test_node_summary_contains_decision_codes_but_not_draft_text():
    summary = summarize_node_result(
        "response_validator",
        ValidationResult(
            passed=True,
            confidence=0.9,
            reason_codes=("GROUNDED",),
            assurance="reduced_assurance",
        ),
    )
    assert summary["decision_summary"] == "response accepted"
    assert summary["reason_codes"] == ["GROUNDED"]
    assert "text" not in summary
```

Add rejection tests for secret-like keys, raw prompts, exception bodies,
reasoning content, and tool argument values.

- [ ] **Step 2: Run telemetry tests and verify failure**

Run:

```powershell
uv run pytest tests/unit/test_operation_telemetry.py tests/unit/pipeline/test_turn.py -q
```

Expected: FAIL because the telemetry boundary and result summarizer do not
exist.

- [ ] **Step 3: Implement node-bound telemetry**

Use a `ContextVar` so concurrent evidence calls inherit the correct node while
remaining isolated:

```python
@dataclass(frozen=True)
class NodeTraceContext:
    trace_id: UUID
    span_id: UUID
    tenant_id: str
    node: str
    attempt: int


class OperationTelemetry:
    def __init__(self, traces) -> None:
        self.traces = traces
        self._current: ContextVar[NodeTraceContext | None] = ContextVar(
            "agent_flow_node_trace", default=None
        )

    @asynccontextmanager
    async def bind_node(self, context: NodeTraceContext):
        token = self._current.set(context)
        try:
            yield
        finally:
            self._current.reset(token)
```

Every recorder requires an active context, builds a fixed allowlisted payload,
applies `sanitize_trace_value`, and appends one event linked to the active span.
Use `component="model"`, `"rag"`, or `"tool"` and event types `model_call`,
`rag_call`, or `tool_call`. A missing active context is a no-op so standalone
model inventory probes do not create trace rows.

- [ ] **Step 4: Instrument adapters without changing their public results**

Add an optional `telemetry: OperationTelemetry | None = None` constructor
argument to `ModelGateway`, `MockRagClient`, and `MockToolClient`. Measure with
`time.monotonic()`. Record model role/profile/model, duration, token counts,
finish reason, and status. Record RAG query fingerprint, result count, source
IDs, and freshness. Record tool name, canonical argument fingerprint, duration,
result source ID, and freshness.

On failure record only the classified safe error code/component/operation; then
re-raise the original exception. Never include request messages, returned
content, raw arguments, response bodies, or exception strings.

- [ ] **Step 5: Add bounded node result summaries**

Implement `summarize_node_result` as an explicit node-name dispatch. It may
return only:

```python
{
    "decision_summary": str,
    "reason_codes": list[str],
    "assurance": str,
    "intent": str,
    "emotion_category": str,
    "tool_names": list[str],
    "evidence_ids": list[str],
    "citations": list[str],
}
```

Unknown nodes return `{}`. Response generation summaries include citation and
evidence IDs but never draft text. Validation summaries include passed/failed,
confidence, assurance, and reason codes. Truncate lists to 20 and strings to
256 characters.

Bind telemetry around the operation inside `TurnPipeline.run_node`. Merge the
safe node summary into the completed lifecycle event after the typed result is
available. Expand `_validated_trace_metadata` only for these exact public keys;
keep immutable artifact-reference validation unchanged.

- [ ] **Step 6: Run telemetry and pipeline regressions**

Run:

```powershell
uv run pytest tests/unit/test_operation_telemetry.py tests/unit/pipeline/test_turn.py tests/e2e/test_turn_pipeline.py tests/unit/test_secret_filter.py -q
```

Expected: PASS; trace events expose structured decisions and operation metadata
without hidden reasoning or secret-bearing values.

- [ ] **Step 7: Commit**

```powershell
git add src/agent_flow/observability.py src/agent_flow/adapters/models.py src/agent_flow/adapters/evidence.py src/agent_flow/pipeline/turn.py tests/unit/test_operation_telemetry.py tests/unit/pipeline/test_turn.py tests/e2e/test_turn_pipeline.py
git commit -m "feat: trace safe operation telemetry"
```

---

### Task 6: Add submission, trace-list, and queued-retry APIs

**Files:**
- Create: `src/agent_flow/inbound.py`
- Create: `src/agent_flow/api/submissions.py`
- Modify: `src/agent_flow/api/dependencies.py`
- Modify: `src/agent_flow/api/turns.py`
- Modify: `src/agent_flow/api/traces.py`
- Modify: `src/agent_flow/main.py`
- Modify: `src/agent_flow/repositories/traces.py`
- Modify: `tests/e2e/test_api.py`
- Modify: `tests/e2e/test_manual_retry.py`

**Interfaces:**
- Produces: `InboundMessageService.submit(context, message) -> SubmissionReceipt`
- Produces: `InboundMessageService.wait(submission_id, scope, timeout) -> SubmissionResult | None`
- Produces: `POST /api/v1/submissions`, `GET /api/v1/submissions/{id}`, and `GET /api/v1/traces`
- Consumes: `PostgresSubmissionRepository`, `AuthenticatedPrincipal`, and existing trace/retry contracts

- [ ] **Step 1: Write failing API authorization and idempotency tests**

```python
async def test_submission_uses_customer_from_bearer_token(client):
    response = await client.post(
        "/api/v1/submissions",
        headers={"Authorization": "Bearer customer"},
        json={
            "channel": "console",
            "external_message_id": "m1",
            "session_id": "s1",
            "text": "你好",
            "idempotency_key": "m1",
            "metadata": {"source": "trace-console"},
        },
    )
    assert response.status_code == 202
    body = response.json()
    assert set(body) == {"submission_id", "trace_id", "status"}


async def test_submission_rejects_missing_scope(client):
    response = await client.post(
        "/api/v1/submissions",
        headers={"Authorization": "Bearer trace-only"},
        json=submission_payload(),
    )
    assert response.status_code == 403


async def test_submission_replay_returns_same_receipt(client):
    payload = submission_payload(idempotency_key="same")
    first = await client.post(
        "/api/v1/submissions",
        headers={"Authorization": "Bearer customer"},
        json=payload,
    )
    second = await client.post(
        "/api/v1/submissions",
        headers={"Authorization": "Bearer customer"},
        json=payload,
    )
    assert second.status_code == 202
    assert second.json() == first.json()


async def test_other_customer_cannot_read_submission(client):
    created = await create_submission(client, token="customer")
    response = await client.get(
        f"/api/v1/submissions/{created['submission_id']}",
        headers={"Authorization": "Bearer internal-c2"},
    )
    assert response.status_code == 404


async def test_trace_list_is_tenant_and_customer_scoped(client):
    await create_submission(client, token="customer")
    response = await client.get(
        "/api/v1/traces",
        headers={"Authorization": "Bearer internal-c2"},
    )
    assert response.status_code == 200
    assert response.json()["items"] == []
```

The request schema must contain no `customer_id`.

- [ ] **Step 2: Run API tests and verify failure**

Run:

```powershell
uv run pytest tests/e2e/test_api.py -q
```

Expected: FAIL with missing submission and trace-list routes.

- [ ] **Step 3: Implement the application service and routes**

Define:

```python
class InboundMessageService:
    def __init__(self, submissions, *, poll_interval: float = 0.1) -> None:
        if poll_interval <= 0:
            raise ValueError("poll interval must be positive")
        self.submissions = submissions
        self.poll_interval = poll_interval

    async def submit(
        self,
        context: AuthorizedCustomerContext,
        message: InboundMessage,
        *,
        retry_of_trace_id: UUID | None = None,
        retry_initiator: str | None = None,
        retry_reason: str | None = None,
        delivery_disposition: str | None = None,
    ) -> SubmissionReceipt:
        record = await self.submissions.enqueue(
            context,
            message,
            retry_of_trace_id=retry_of_trace_id,
            retry_initiator=retry_initiator,
            retry_reason=retry_reason,
            delivery_disposition=delivery_disposition,
        )
        return SubmissionReceipt(
            submission_id=record.id,
            trace_id=record.trace_id,
            status=record.status,
        )

    async def get(
        self, submission_id: UUID, context: AuthorizedCustomerContext
    ) -> SubmissionResult | None:
        record = await self.submissions.get(
            submission_id,
            tenant_id=context.tenant_id,
            customer_id=context.customer_id,
        )
        return None if record is None else record.to_result()

    async def wait(
        self,
        submission_id: UUID,
        context: AuthorizedCustomerContext,
        *,
        timeout: float,
    ) -> SubmissionResult | None:
        try:
            async with asyncio.timeout(timeout):
                while True:
                    result = await self.get(submission_id, context)
                    if result is None or result.status in {"completed", "failed"}:
                        return result
                    await asyncio.sleep(self.poll_interval)
        except TimeoutError:
            return None
```

Add `submissions` and `inbound` fields to `AppServices`, and add keyword-only
`submissions=None, inbound=None` parameters to `create_app(...)` so existing
injected test apps remain valid. The submission route requires `turn:write`,
derives `AuthorizedCustomerContext` from the principal, and returns status 202.

Add a cursor-paginated repository method and route:

```python
async def list_traces(
    self,
    *,
    tenant_id: str,
    customer_id: str | None,
    status: str | None,
    before_created_at: datetime | None,
    before_id: UUID | None,
    limit: int,
) -> tuple[TraceRecord, ...]:
    if not 1 <= limit <= 100:
        raise ValueError("trace list limit must be between 1 and 100")
    if (before_created_at is None) != (before_id is None):
        raise ValueError("trace cursor time and id must be provided together")
    clauses = ["tenant_id = %s"]
    values: list[object] = [tenant_id]
    if customer_id is not None:
        clauses.append("customer_id = %s")
        values.append(customer_id)
    if status is not None:
        clauses.append("status = %s")
        values.append(status)
    if before_created_at is not None and before_id is not None:
        clauses.append("(created_at, id) < (%s, %s)")
        values.extend((before_created_at, before_id))
    values.append(limit)
    return await self._load_trace_records(
        where_sql=" AND ".join(clauses),
        parameters=tuple(values),
        suffix_sql="ORDER BY created_at DESC, id DESC LIMIT %s",
    )
```

The API encodes `(created_at, id)` as URL-safe base64 JSON and rejects malformed
cursors with 422. It passes both decoded values to the repository and emits the
last row as `next_cursor`. Cap `limit` at 100.

- [ ] **Step 4: Route legacy turns through the same queue**

Keep the current `SubmitTurn` request shape. Construct an `InboundMessage` with
`channel="console"` and a generated external/idempotency ID. Wait for the
configured bounded timeout. Return the existing terminal `TurnResult` shape
when completed. On timeout, return 202 with `Location:
/api/v1/submissions/{id}` and the receipt.

Do not call `pipeline.run` from `api/turns.py`.

- [ ] **Step 5: Queue manual retries**

Rewrite `manual_retry` to retain all current authorization, expiry, artifact,
and retry-limit checks, but enqueue through `InboundMessageService.submit(...)`
with:

```python
retry_message = InboundMessage(
    channel="console",
    external_message_id=f"retry:{trace.id}:{payload.idempotency_key}",
    session_id=captured.request.session_id,
    text=captured.request.message,
    case_id=captured.request.case_id,
    idempotency_key=payload.idempotency_key,
    metadata={"source": "manual-retry"},
)
receipt = await app_services.inbound.submit(
    context,
    retry_message,
    retry_of_trace_id=trace.id,
    retry_initiator=authenticated.subject_id,
    retry_reason=payload.reason,
    delivery_disposition="review_required",
)
```

Add required `idempotency_key` to `ManualRetryRequest` with the same 1–256
character bounds as `InboundMessage`. The console generates it with
`crypto.randomUUID()`. An identical retry replay returns the existing receipt.

Return the new queued trace ID and `delivery_disposition="review_required"`.
Do not run the retry pipeline in the API process.

- [ ] **Step 6: Run API and retry tests**

Run:

```powershell
uv run pytest tests/e2e/test_api.py tests/e2e/test_manual_retry.py tests/unit/test_auth.py -q
```

Expected: PASS, including cross-tenant and cross-customer 404 behavior.

- [ ] **Step 7: Commit**

```powershell
git add src/agent_flow/inbound.py src/agent_flow/api/submissions.py src/agent_flow/api/dependencies.py src/agent_flow/api/turns.py src/agent_flow/api/traces.py src/agent_flow/main.py src/agent_flow/repositories/traces.py tests/e2e/test_api.py tests/e2e/test_manual_retry.py
git commit -m "feat: expose queued inbound submissions"
```

---

### Task 7: Build the runnable demo composition root

**Files:**
- Create: `src/agent_flow/runtime.py`
- Modify: `src/agent_flow/config.py`
- Modify: `src/agent_flow/auth.py`
- Modify: `src/agent_flow/main.py`
- Modify: `src/agent_flow/api/health.py`
- Modify: `src/agent_flow/worker.py`
- Create: `config/demo/rag.json`
- Create: `config/demo/tools.json`
- Modify: `.env.example`
- Modify: `compose.yaml`
- Create: `tests/unit/test_runtime.py`
- Modify: `tests/e2e/test_bootstrap_acceptance.py`
- Modify: `tests/e2e/test_compose_config.py`

**Interfaces:**
- Produces: `build_demo_components(settings, pool) -> RuntimeComponents`
- Produces: `demo_lifespan(app)` and `create_runtime_app()`
- Produces: `DemoTokenAuthenticator.__call__(token) -> AuthenticatedPrincipal | None`
- Consumes: all repositories, `ModelRegistry`, `ModelGateway`, artifacts, mock evidence adapters, and the worker from prior tasks

- [ ] **Step 1: Write failing runtime and token tests**

```python
async def test_demo_authenticator_uses_constant_time_static_tokens(settings):
    auth = DemoTokenAuthenticator.from_settings(settings)
    principal = await auth(settings.demo_customer_token.get_secret_value())
    assert principal == AuthenticatedPrincipal(
        subject_id="demo-customer",
        tenant_id=settings.demo_tenant_id,
        customer_id=settings.demo_customer_id,
        scopes=frozenset({"turn:write", "trace:read", "trace:retry"}),
    )
    assert await auth("wrong") is None


async def test_demo_lifespan_opens_and_closes_pool_once(
    runtime_app, fake_pool
):
    async with runtime_app.router.lifespan_context(runtime_app):
        assert fake_pool.open_calls == 1
        assert runtime_app.state.services.pipeline is not None
    assert fake_pool.close_calls == 1


async def test_runtime_builds_real_pipeline_and_mock_evidence_adapters(
    settings, opened_pool, fake_inventory_probe
):
    components = await build_demo_components(
        settings, opened_pool, inventory_probe=fake_inventory_probe
    )
    assert isinstance(components.pipeline, TurnPipeline)
    assert isinstance(components.pipeline.rag, MockRagClient)
    assert isinstance(components.pipeline.tools, MockToolClient)
    assert (
        components.pipeline.models.registry.resolve("response_generator").model
        == "Qwen/Qwen3-8B-AWQ"
    )


async def test_required_probe_failure_marks_models_not_ready_without_content(
    runtime_app, fake_inventory_probe
):
    fake_inventory_probe.failure = RuntimeError(
        "private model output must not appear"
    )
    async with runtime_app.router.lifespan_context(runtime_app):
        response = await call_ready(runtime_app)
    assert response.status_code == 503
    body = response.json()
    assert body["checks"]["models"]["error_code"] == "MODEL_CAPABILITY_FAILED"
    assert "private model output" not in json.dumps(body)
```

Also assert production mode rejects demo tokens instead of accepting them.

- [ ] **Step 2: Run runtime tests and verify failure**

Run:

```powershell
uv run pytest tests/unit/test_runtime.py tests/e2e/test_bootstrap_acceptance.py -q
```

Expected: FAIL because no runtime composition root exists.

- [ ] **Step 3: Add explicit demo settings and authentication**

Add settings:

```python
app_runtime_mode: Literal["demo", "production"] = "demo"
demo_customer_token: SecretStr = Field(min_length=16)
demo_admin_token: SecretStr = Field(min_length=16)
demo_tenant_id: str = "t1"
demo_customer_id: str = "c1"
demo_rag_fixture: Path = Path("config/demo/rag.json")
demo_tool_fixture: Path = Path("config/demo/tools.json")
legacy_turn_timeout_seconds: float = Field(default=60.0, gt=0, le=300)
```

Use `hmac.compare_digest` in `DemoTokenAuthenticator`. Customer and admin
principals have explicit, different scopes. Never include `SecretStr` values in
repr, errors, health details, or logs. `DemoTokenAuthenticator.from_settings`
raises `RuntimeError("demo authentication is disabled")` when mode is
`production`; the composition root also rejects production mode until a
production authenticator is explicitly implemented.

- [ ] **Step 4: Implement runtime components and lifespan**

Define:

```python
@dataclass(frozen=True)
class RuntimeComponents:
    pool: PostgresPool
    traces: PostgresTraceRepository
    conversations: PostgresConversationRepository
    submissions: PostgresSubmissionRepository
    inbound: InboundMessageService
    pipeline: TurnPipeline
    authenticate: Authenticator
    inventory: dict[str, InventoryResult]
```

`build_demo_components` loads artifacts and model configuration, creates one
registry shared by `ModelGateway` and `EmbeddingModel`, builds mock RAG/tool
clients from `config/demo`, creates one `OperationTelemetry` bound to the trace
repository, injects it into the model/RAG/tool adapters and pipeline, and
constructs the real pipeline. Use a small
`SystemClock.now() -> datetime` implementation returning timezone-aware UTC.

The FastAPI lifespan opens the pool, runs all required inventory/capability
probes, installs a complete `AppServices`, yields, and closes the pool. Convert
probe failures into bounded readiness details:

```json
{
  "status": "not_ready",
  "checks": {
    "models": {
      "status": "unavailable",
      "role": "dialogue_classifier",
      "stage": "capability",
      "error_code": "MODEL_CAPABILITY_FAILED"
    }
  }
}
```

Replace string-only dependency checks with this strict public shape:

```python
class ReadinessCheck(BaseModel):
    status: Literal["ok", "missing", "invalid", "unavailable"]
    role: str | None = None
    stage: str | None = None
    error_code: str | None = None
```

`AppServices.dependency_checks` becomes `dict[str, ReadinessCheck]`. Health is
ready only when every check has `status == "ok"`. Existing callers that pass a
string in tests are normalized to `ReadinessCheck(status=value)` at
`create_app(...)`.

Do not include exception strings or model output. Unsupported runtime modes
raise a startup configuration error; they do not fall back to a shell.

- [ ] **Step 5: Compose the worker from the same builder**

Refactor worker startup to open its pool, build the same demo components, run
the required probes, create `TurnJobWorker`, and start turn, outbox, and
retention loops in one `TaskGroup`. The Compose `worker` service remains one
instance.

- [ ] **Step 6: Add demo fixtures and Compose configuration**

Copy the minimum authorized success and timeout scenarios from existing test
fixtures into `config/demo`. Use tenant `${DEMO_TENANT_ID:-t1}` and customer
`${DEMO_CUSTOMER_ID:-c1}`.

Add to app and worker environments:

```yaml
APP_RUNTIME_MODE: demo
DEMO_CUSTOMER_TOKEN: ${DEMO_CUSTOMER_TOKEN}
DEMO_ADMIN_TOKEN: ${DEMO_ADMIN_TOKEN}
DEMO_TENANT_ID: ${DEMO_TENANT_ID:-t1}
DEMO_CUSTOMER_ID: ${DEMO_CUSTOMER_ID:-c1}
```

Keep the host gateway for local vLLM. Set the remote default only through
`.env`; do not embed the internal host or API key in Compose.

Before the runtime checkpoint, add both missing demo tokens to the ignored local
`.env` with at least 16 characters each. Do not copy them into `.env.example`,
Compose defaults, test output, commits, or review files.

- [ ] **Step 7: Run the core automated checkpoint**

Run:

```powershell
uv run pytest tests/unit/test_runtime.py tests/e2e/test_bootstrap_acceptance.py tests/e2e/test_compose_config.py -q
uv run pytest -q
docker compose config
docker compose build app worker migrate
```

Expected: all tests PASS with only documented environment/database skips;
Compose config resolves without printing secret values in test output.

- [ ] **Step 8: Commit**

```powershell
git add src/agent_flow/runtime.py src/agent_flow/config.py src/agent_flow/auth.py src/agent_flow/main.py src/agent_flow/api/health.py src/agent_flow/worker.py config/demo/rag.json config/demo/tools.json .env.example compose.yaml tests/unit/test_runtime.py tests/e2e/test_bootstrap_acceptance.py tests/e2e/test_compose_config.py
git commit -m "feat: compose runnable demo runtime"
```

#### Core Runtime Checkpoint

Before frontend work, start PostgreSQL, migration, app, and worker; verify
`/health/ready`; submit one Chinese message to `/api/v1/submissions`; poll the
submission and trace events to a terminal response or explicit handoff. Record
only statuses, identifiers, role names, dimensions, durations, and error codes.
Never print credentials, model content, or hidden reasoning during diagnostics.

---

### Task 8: Package the console shell and in-memory token gate

**Files:**
- Create: `src/agent_flow/console/index.html`
- Create: `src/agent_flow/console/styles.css`
- Create: `src/agent_flow/console/api.js`
- Modify: `src/agent_flow/main.py`
- Modify: `pyproject.toml`
- Create: `tests/unit/test_console_assets.py`

**Interfaces:**
- Produces: `/console/` and packaged static assets
- Produces: `ApiClient` with memory-only `setToken`, `clearToken`, and authenticated `request`
- Consumes: same-origin `/api/v1` endpoints

- [ ] **Step 1: Write failing static-asset tests**

```python
def test_console_and_declared_assets_are_served(app_client):
    assert app_client.get("/console/").status_code == 200
    assert app_client.get("/console/styles.css").headers["content-type"].startswith(
        "text/css"
    )
    assert app_client.get("/console/api.js").headers["content-type"].startswith(
        "text/javascript"
    )


def test_console_does_not_expose_parent_files(app_client):
    response = app_client.get("/console/%2e%2e/%2e%2e/.env")
    assert response.status_code in {404, 405}
    assert "DATABASE_URL" not in response.text
```

- [ ] **Step 2: Run and verify failure**

Run:

```powershell
uv run pytest tests/unit/test_console_assets.py -q
```

Expected: FAIL because `/console/` is not mounted.

- [ ] **Step 3: Add the accessible shell and static mount**

Create semantic regions with stable IDs:

```html
<header id="app-header"></header>
<dialog id="token-dialog" aria-labelledby="token-title"></dialog>
<main>
  <aside id="trace-list" aria-label="Trace records"></aside>
  <section id="trace-workspace" aria-live="polite"></section>
</main>
<aside id="simulator-panel" hidden></aside>
<div id="alert-region" role="alert" aria-live="assertive"></div>
```

Mount `StaticFiles(packages=[("agent_flow", "console")], html=True)` at
`/console`. Include console assets in the wheel through Hatch configuration.
Keep API and health routers unchanged.

- [ ] **Step 4: Implement memory-only authentication**

`api.js` must hold the token in a module-scoped variable only:

```javascript
let bearerToken = "";

export function setToken(value) {
  bearerToken = String(value || "").trim();
}

export function clearToken() {
  bearerToken = "";
}

export async function request(path, options = {}) {
  if (!bearerToken) throw new Error("authentication required");
  const headers = new Headers(options.headers || {});
  headers.set("Authorization", `Bearer ${bearerToken}`);
  headers.set("Accept", "application/json");
  const response = await fetch(path, {...options, headers});
  if (response.status === 401) clearToken();
  return response;
}
```

Do not read or write `localStorage`, `sessionStorage`, `document.cookie`, query
parameters, or URL fragments.

- [ ] **Step 5: Style the dark trace-first layout**

Use CSS custom properties with off-white text on layered blue-gray surfaces.
Every status needs an icon/text label in addition to color. Add visible
`:focus-visible` rings, a horizontally scrollable flow region, and responsive
stacking without changing text to black.

- [ ] **Step 6: Run asset and packaging tests**

Run:

```powershell
uv run pytest tests/unit/test_console_assets.py -q
uv build
```

Expected: PASS and the wheel contains every console asset.

- [ ] **Step 7: Commit**

```powershell
git add src/agent_flow/console/index.html src/agent_flow/console/styles.css src/agent_flow/console/api.js src/agent_flow/main.py pyproject.toml tests/unit/test_console_assets.py uv.lock
git commit -m "feat: serve trace console shell"
```

---

### Task 9: Render scoped traces, live events, and error details

**Files:**
- Create: `src/agent_flow/console/state.js`
- Create: `src/agent_flow/console/render.js`
- Create: `src/agent_flow/console/app.js`
- Modify: `src/agent_flow/console/index.html`
- Modify: `src/agent_flow/console/styles.css`
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Create: `tests/browser/conftest.py`
- Create: `tests/browser/test_trace_console.py`

**Interfaces:**
- Produces: `createState()`, `mergeEvents(state, events)`, and `toggleNode(state, nodeKey)`
- Produces: safe renderers for trace list, incident strip, flow nodes, details, and lineage
- Consumes: trace list/detail/event/retry APIs through `ApiClient`

- [ ] **Step 1: Write failing browser tests for trace rendering**

Install the browser-only development dependency:

```powershell
uv add --dev "playwright>=1.55,<2"
```

Using Python Playwright with mocked same-origin API responses, assert:

```python
def test_failed_node_opens_and_shows_exact_location(page, console_url):
    install_api_fixture(page, scenario="tool-timeout")
    page.goto(console_url)
    authenticate(page)
    page.get_by_role("button", name="tool timeout trace").click()
    failed = page.locator('[data-node="evidence_collector"][data-status="failed"]')
    expect(failed).to_have_attribute("aria-expanded", "true")
    expect(failed.locator("[data-error-code]")).to_have_text("TOOL_TIMEOUT")
    expect(failed.locator("[data-component]")).to_have_text("order_api")
    expect(failed.locator("[data-operation]")).to_have_text("order.lookup")
```

Create `tests/browser/conftest.py` with a session-scoped FastAPI/uvicorn server,
a Chromium `page` fixture, and route helpers that fulfill JSON responses for
trace list/detail/events, submission status, and retry. Each helper must also
register `page.on("console")` and fail teardown when a browser error was
observed. Fixtures use synthetic IDs and safe summaries only; they never read
`.env`.

Also cover multiple expanded nodes, keyboard activation, empty lists, stale
polling state, and event deduplication by sequence.

- [ ] **Step 2: Run and verify failure**

Run:

```powershell
uv run pytest tests/browser/test_trace_console.py -q
```

Expected: FAIL because state and rendering modules do not exist. If Chromium is
not installed, install it with `uv run playwright install chromium`, then rerun.

- [ ] **Step 3: Implement explicit state transitions**

State contains:

```javascript
{
  authenticated: false,
  traces: [],
  selectedTraceId: null,
  selectedTrace: null,
  selectedAttemptId: null,
  eventCursor: 0,
  expandedNodes: new Set(),
  filters: {status: "", before: null},
  polling: {active: false, stale: false, failures: 0},
  simulator: {open: false, submission: null, messages: []}
}
```

`mergeEvents` ignores sequences at or below the cursor, sorts new events by
sequence, and never duplicates them. Selecting a trace opens failed nodes by
default. Retrying never removes the source trace.

- [ ] **Step 4: Implement allowlisted DOM rendering**

Render dynamic data with `document.createElement` and `textContent`. Define
explicit allowlists:

```javascript
const DETAIL_FIELDS = new Set([
  "decision_summary", "reason_codes", "model_role", "model_profile",
  "duration_ms", "input_tokens", "output_tokens", "tool",
  "freshness_seconds", "attempt", "delivery_disposition",
  "error_code", "failure_stage", "component", "operation",
  "artifact_id", "semantic_version", "checksum", "sequence", "created_at"
]);
```

Unknown fields render only the text `Additional safe metadata unavailable`.
Never recursively dump objects. Each node button controls a downward detail
panel through matching `aria-controls` and `aria-expanded`.

- [ ] **Step 5: Implement bounded polling**

Poll selected trace events after `eventCursor`. Use 1 second while running and 5
seconds while terminal/idle. On failure, retain existing data and use bounded
backoff of 2, 4, 8, then 15 seconds. Mark the view stale after the first
failure. Stop timers on logout and page unload.

- [ ] **Step 6: Run browser and API regression tests**

Run:

```powershell
uv run pytest tests/browser/test_trace_console.py tests/e2e/test_api.py -q
```

Expected: PASS with no browser console errors.

- [ ] **Step 7: Commit**

```powershell
git add src/agent_flow/console/state.js src/agent_flow/console/render.js src/agent_flow/console/app.js src/agent_flow/console/index.html src/agent_flow/console/styles.css tests/browser/conftest.py tests/browser/test_trace_console.py pyproject.toml uv.lock
git commit -m "feat: render live trace incidents"
```

---

### Task 10: Add the inbound simulator and manual retry UX

**Files:**
- Modify: `src/agent_flow/console/index.html`
- Modify: `src/agent_flow/console/styles.css`
- Modify: `src/agent_flow/console/api.js`
- Modify: `src/agent_flow/console/state.js`
- Modify: `src/agent_flow/console/render.js`
- Modify: `src/agent_flow/console/app.js`
- Modify: `tests/browser/test_trace_console.py`

**Interfaces:**
- Produces: collapsible simulator submission flow and safe terminal transcript
- Produces: manual retry dialog and immutable lineage navigation
- Consumes: submission create/status, trace event, and manual retry APIs

- [ ] **Step 1: Write failing simulator and retry browser tests**

```python
def test_simulator_selects_trace_and_waits_for_safe_terminal_reply(
    page, console_url
):
    install_submission_sequence(page, statuses=["queued", "running", "completed"])
    page.goto(console_url)
    authenticate(page)
    page.get_by_role("button", name="Open message simulator").click()
    page.get_by_label("Message").fill("我的訂單在哪裡？")
    page.get_by_role("button", name="Send message").click()
    expect(page.locator("[data-selected-trace]")).to_have_text("trace-001")
    expect(page.get_by_text("處理中")).to_be_visible()
    expect(page.get_by_text("訂單正在配送中")).to_be_visible()


def test_manual_retry_preserves_source_and_selects_new_attempt(
    page, console_url
):
    install_retry_fixture(
        page, source_trace="trace-001", retry_trace="trace-002"
    )
    page.goto(console_url)
    authenticate(page)
    page.get_by_role("button", name="Retry trace").click()
    page.get_by_label("Retry reason").fill("operator requested retry")
    page.get_by_role("button", name="Confirm retry").click()
    expect(page.locator('[data-trace-id="trace-001"]')).to_be_visible()
    expect(page.locator('[data-trace-id="trace-002"]')).to_be_visible()
    expect(page.locator("[data-selected-trace]")).to_have_text("trace-002")


def test_handoff_renders_safe_message_without_partial_draft(
    page, console_url
):
    install_submission_sequence(
        page,
        statuses=["queued", "running", "completed"],
        handoff={
            "required": True,
            "reason_code": "HIGH_RISK",
            "safe_message": "已轉交人工協助",
        },
        forbidden_partial_text="unvalidated draft",
    )
    page.goto(console_url)
    authenticate(page)
    submit_message(page, "我需要人工協助")
    expect(page.get_by_text("已轉交人工協助")).to_be_visible()
    expect(page.get_by_text("unvalidated draft")).to_have_count(0)


def test_refresh_clears_token_and_returns_to_token_dialog(
    page, console_url
):
    page.goto(console_url)
    authenticate(page)
    page.reload()
    expect(
        page.get_by_role("dialog", name="Demo authentication")
    ).to_be_visible()
```

- [ ] **Step 2: Run and verify failure**

Run:

```powershell
uv run pytest tests/browser/test_trace_console.py -q
```

Expected: FAIL on missing simulator and retry interactions.

- [ ] **Step 3: Implement simulator submission**

Generate external and idempotency IDs with `crypto.randomUUID()`. Submit:

```javascript
{
  channel: "console",
  external_message_id: messageId,
  session_id: state.simulator.sessionId,
  text,
  idempotency_key: messageId,
  metadata: {source: "trace-console"}
}
```

Immediately select the returned trace. Poll submission status separately from
trace events. Render only user-entered text, queued/running labels, terminal
`text` and citations, or the safe handoff message. Do not display intermediate
model drafts. If lease recovery changes the submission's current `trace_id`,
retain the abandoned trace in lineage, switch event polling to the replacement
trace, and show `WORKER_LEASE_EXPIRED` on the abandoned attempt.

- [ ] **Step 4: Implement manual retry and lineage**

Enable retry only for terminal traces, require a trimmed non-empty reason, and
disable after three retry descendants. After a 202 response, retain the source
attempt, append/select the new queued trace, and display
`delivery_disposition="review_required"`.

- [ ] **Step 5: Run browser tests**

Run:

```powershell
uv run pytest tests/browser/test_trace_console.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```powershell
git add src/agent_flow/console/index.html src/agent_flow/console/styles.css src/agent_flow/console/api.js src/agent_flow/console/state.js src/agent_flow/console/render.js src/agent_flow/console/app.js tests/browser/test_trace_console.py
git commit -m "feat: add inbound message simulator"
```

---

### Task 11: Document, verify, and run the complete demo

**Files:**
- Modify: `README.md`
- Modify: `.env.example`
- Modify: `compose.yaml`
- Modify: `tests/e2e/test_compose_config.py`
- Create: `docs/reviews/2026-07-23-live-trace-console-review.md`

**Interfaces:**
- Consumes: the complete runtime, worker, API, and console
- Produces: reproducible installation, model configuration, operation, retry, and troubleshooting instructions

- [ ] **Step 1: Write failing documentation-contract tests**

Add assertions that README documents:

- `uv sync --frozen`
- local vLLM `Qwen/Qwen3-8B-AWQ` on `localhost:8000`
- `--max-model-len 6144`
- remote OpenAI-compatible structured and embedding endpoints
- flexible model role replacement
- demo token setup and its demo-only warning
- `docker compose up --build`
- readiness, submission, trace, event, retry, and console checks
- queue recovery and exact failure-location troubleshooting
- future LINE adapter boundary

Run:

```powershell
uv run pytest tests/e2e/test_compose_config.py -q
```

Expected: FAIL until README and Compose commands match the runnable system.

- [ ] **Step 2: Update README and environment examples**

Document configuration without real internal hosts, API keys, or token values.
Include PowerShell examples that read credentials from environment variables
instead of echoing them. Explain that model role names are stable while profile
and model names are replaceable.

Add a troubleshooting table mapping:

```text
readiness check -> model role -> probe stage -> trace node -> component ->
operation -> safe error code -> automatic/manual retry disposition
```

- [ ] **Step 3: Run all automated verification**

Run:

```powershell
uv run pytest -q
uv build
docker compose config
docker compose build
```

Expected: complete test suite PASS with only explicit environment/database
skips; wheel build and every Compose image build succeed.

- [ ] **Step 4: Run the real local demo**

With `.env` populated locally:

```powershell
docker compose up -d postgres
docker compose run --rm migrate
docker compose up -d app worker
```

Verify without printing secret-bearing requests:

```powershell
Invoke-RestMethod http://localhost:8080/health/live
Invoke-RestMethod http://localhost:8080/health/ready
```

Open `http://localhost:8080/console/`, enter the demo token manually, submit a
Chinese message, and confirm the trace reaches a safe response or explicit
handoff. Confirm the error scenario displays node, component, operation, safe
error code, job attempt, and retry lineage.

- [ ] **Step 5: Review the implementation**

Use Claude CLI with the configured Opus 4.8 alias when available and direct it
to write its findings to
`docs/reviews/2026-07-23-live-trace-console-review.md` without interaction. If
the CLI is unavailable or rate-limited, perform the same review locally. Review
for spec coverage, authorization binding, queue races, secret leakage, retry
semantics, model capability mismatches, DOM injection, and overengineering.

Resolve every blocking or high-severity finding with a failing regression test
before changing implementation. Re-run the focused test and full suite.

- [ ] **Step 6: Commit final documentation and review**

```powershell
git add README.md .env.example compose.yaml tests/e2e/test_compose_config.py docs/reviews/2026-07-23-live-trace-console-review.md
git commit -m "docs: complete live trace console demo"
```
