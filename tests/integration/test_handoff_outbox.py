import asyncio
import hashlib
import hmac
import json
import time
from datetime import datetime, timezone
from uuid import uuid4

import httpx
import pytest
import respx

from agent_flow.adapters.webhook import HandoffWebhook, WebhookDeliveryError
from agent_flow.contracts import HandoffEvent
from agent_flow.repositories.outbox import OutboxRepository
from agent_flow.worker import HandoffOutboxWorker
from conftest import _is_unambiguous_test_database, require_unambiguous_test_database


@pytest.fixture
def handoff() -> HandoffEvent:
    return HandoffEvent(
        required=True,
        reason_code="ACCOUNT_SECURITY",
        safe_message="A specialist will review this request.",
    )


def test_destructive_cleanup_database_guard_is_strict():
    assert _is_unambiguous_test_database("agent_flow_test")
    assert not _is_unambiguous_test_database("contestprod")
    assert not _is_unambiguous_test_database("production")


@pytest.fixture
async def outbox_repository(postgres_pool):
    repository = OutboxRepository(postgres_pool)
    await require_unambiguous_test_database(postgres_pool)
    async with postgres_pool.connection() as connection:
        await connection.execute("TRUNCATE notification.outbox, observability.traces CASCADE")
    yield repository
    await require_unambiguous_test_database(postgres_pool)
    async with postgres_pool.connection() as connection:
        await connection.execute("TRUNCATE notification.outbox, observability.traces CASCADE")


async def _trace(trace_repository, tenant_id="tenant-a", customer_id="customer-a"):
    return await trace_repository.start_trace(
        tenant_id=tenant_id, customer_id=customer_id, session_id=f"session-{uuid4()}"
    )


@pytest.mark.asyncio
async def test_duplicate_enqueue_returns_same_immutable_record(
    outbox_repository, trace_repository, handoff
):
    trace_id = await _trace(trace_repository)
    first = await outbox_repository.enqueue(
        tenant_id="tenant-a",
        customer_id="customer-a",
        trace_id=trace_id,
        idempotency_key=f"handoff:{trace_id}",
        event=handoff,
    )
    second = await outbox_repository.enqueue(
        tenant_id="tenant-a",
        customer_id="customer-a",
        trace_id=trace_id,
        idempotency_key=f"handoff:{trace_id}",
        event=handoff,
    )

    assert second == first
    assert await outbox_repository.count(tenant_id="tenant-a") == 1
    with pytest.raises(ValueError, match="idempotency key conflicts"):
        await outbox_repository.enqueue(
            tenant_id="tenant-a",
            customer_id="customer-a",
            trace_id=trace_id,
            idempotency_key=f"handoff:{trace_id}",
            event=handoff.model_copy(update={"reason_code": "SELF_HARM"}),
        )


@pytest.mark.asyncio
async def test_claims_are_exclusive_ordered_and_owner_settlement_is_enforced(
    outbox_repository, trace_repository, handoff
):
    first_trace = await _trace(trace_repository)
    second_trace = await _trace(trace_repository)
    first = await outbox_repository.enqueue(
        tenant_id="tenant-a", customer_id="customer-a", trace_id=first_trace,
        idempotency_key=f"handoff:{first_trace}", event=handoff,
    )
    second = await outbox_repository.enqueue(
        tenant_id="tenant-a", customer_id="customer-a", trace_id=second_trace,
        idempotency_key=f"handoff:{second_trace}", event=handoff,
    )

    async with outbox_repository._pool.connection() as connection:
        await connection.execute(
            "UPDATE notification.outbox SET created_at = %s WHERE id IN (%s, %s)",
            (datetime(2026, 1, 1, tzinfo=timezone.utc), first.id, second.id),
        )
    ordered = await outbox_repository.claim(
        owner="ordering", limit=2, lease_seconds=1, max_attempts=3
    )
    assert [row.id for row in ordered] == sorted((first.id, second.id))
    async with outbox_repository._pool.connection() as connection:
        await connection.execute(
            "UPDATE notification.outbox SET lease_expires_at = now() - interval '1 second'"
        )
    claimed_a, claimed_b = await asyncio.gather(
        outbox_repository.claim(owner="worker-a", limit=1, lease_seconds=30, max_attempts=3),
        outbox_repository.claim(owner="worker-b", limit=1, lease_seconds=30, max_attempts=3),
    )
    assert {claimed_a[0].id, claimed_b[0].id} == {first.id, second.id}
    with pytest.raises(ValueError, match="active claim"):
        await outbox_repository.complete(
            claimed_a[0].id, owner="worker-b", claim_token=claimed_a[0].claim_token,
            http_status=204,
        )
    completed = await outbox_repository.complete(
        claimed_a[0].id, owner="worker-a", claim_token=claimed_a[0].claim_token,
        http_status=204,
    )
    assert await outbox_repository.complete(
        claimed_a[0].id, owner="worker-a", claim_token=claimed_a[0].claim_token,
        http_status=204,
    ) == completed
    assert completed.status == "delivered"
    assert completed.last_http_status == 204


@pytest.mark.asyncio
async def test_expired_lease_is_recovered_and_old_owner_cannot_settle(
    outbox_repository, trace_repository, handoff
):
    trace_id = await _trace(trace_repository)
    queued = await outbox_repository.enqueue(
        tenant_id="tenant-a", customer_id="customer-a", trace_id=trace_id,
        idempotency_key=f"handoff:{trace_id}", event=handoff,
    )
    claimed = (await outbox_repository.claim(
        owner="stable-owner", limit=1, lease_seconds=1, max_attempts=3
    ))[0]
    async with outbox_repository._pool.connection() as connection:
        await connection.execute(
            "UPDATE notification.outbox SET lease_expires_at = now() - interval '1 second' WHERE id = %s",
            (queued.id,),
        )
    with pytest.raises(ValueError, match="active claim"):
        await outbox_repository.complete(
            claimed.id, owner="stable-owner", claim_token=claimed.claim_token,
            http_status=204,
        )
    recovered = (await outbox_repository.claim(
        owner="stable-owner", limit=1, lease_seconds=30, max_attempts=3
    ))[0]
    assert recovered.id == claimed.id
    assert recovered.attempts == 2
    assert recovered.claim_token != claimed.claim_token
    with pytest.raises(ValueError, match="claim"):
        await outbox_repository.fail(
            recovered.id, owner="stable-owner", claim_token=claimed.claim_token,
            error_code="WEBHOOK_TIMEOUT",
            http_status=None, retryable=True, max_attempts=3, backoff_seconds=1,
        )
    await outbox_repository.complete(
        recovered.id, owner="stable-owner", claim_token=recovered.claim_token,
        http_status=204,
    )


@pytest.mark.asyncio
async def test_claim_skips_a_locked_first_due_row_without_blocking(
    outbox_repository, trace_repository, handoff
):
    rows = []
    for _ in range(2):
        trace_id = await _trace(trace_repository)
        rows.append(await outbox_repository.enqueue(
            tenant_id="tenant-a", customer_id="customer-a", trace_id=trace_id,
            idempotency_key=f"handoff:{trace_id}", event=handoff,
        ))
    ordered = sorted(rows, key=lambda row: row.id)
    async with outbox_repository._pool.connection() as connection:
        await connection.execute(
            "UPDATE notification.outbox SET created_at = %s WHERE id IN (%s, %s)",
            (datetime(2026, 1, 1, tzinfo=timezone.utc), rows[0].id, rows[1].id),
        )
    async with outbox_repository._pool.connection() as connection:
        async with connection.transaction():
            await connection.execute(
                "SELECT id FROM notification.outbox WHERE id = %s FOR UPDATE",
                (ordered[0].id,),
            )
            started = time.monotonic()
            claimed = await asyncio.wait_for(
                outbox_repository.claim(
                    owner="skip-worker", limit=1, lease_seconds=30, max_attempts=3
                ),
                timeout=1,
            )
            assert time.monotonic() - started < 1
            assert [row.id for row in claimed] == [ordered[1].id]


@pytest.mark.asyncio
async def test_nonretryable_and_exhausted_rows_are_never_claimed_again(
    outbox_repository, trace_repository, handoff
):
    trace_ids = [await _trace(trace_repository) for _ in range(2)]
    for trace_id in trace_ids:
        await outbox_repository.enqueue(
            tenant_id="tenant-a", customer_id="customer-a", trace_id=trace_id,
            idempotency_key=f"handoff:{trace_id}", event=handoff,
        )
    rows = await outbox_repository.claim(
        owner="worker", limit=2, lease_seconds=30, max_attempts=3
    )
    permanent = await outbox_repository.fail(
        rows[0].id, owner="worker", claim_token=rows[0].claim_token,
        error_code="WEBHOOK_400", http_status=400,
        retryable=False, max_attempts=3, backoff_seconds=1,
    )
    exhausted = await outbox_repository.fail(
        rows[1].id, owner="worker", claim_token=rows[1].claim_token,
        error_code="WEBHOOK_503", http_status=503,
        retryable=True, max_attempts=1, backoff_seconds=1,
    )
    assert permanent.status == exhausted.status == "failed"
    assert await outbox_repository.fail(
        rows[0].id, owner="worker", claim_token=rows[0].claim_token,
        error_code="WEBHOOK_400", http_status=400,
        retryable=False, max_attempts=3, backoff_seconds=1,
    ) == permanent
    assert permanent.next_attempt_at is None
    assert exhausted.next_attempt_at is None
    assert await outbox_repository.claim(
        owner="other", limit=10, lease_seconds=30, max_attempts=3
    ) == ()


@pytest.mark.asyncio
async def test_expired_claim_at_attempt_cap_becomes_terminal_without_reclaim(
    outbox_repository, trace_repository, handoff
):
    trace_id = await _trace(trace_repository)
    queued = await outbox_repository.enqueue(
        tenant_id="tenant-a", customer_id="customer-a", trace_id=trace_id,
        idempotency_key=f"handoff:{trace_id}", event=handoff,
    )
    claimed = (await outbox_repository.claim(
        owner="crash-loop", limit=1, lease_seconds=1, max_attempts=1
    ))[0]
    async with outbox_repository._pool.connection() as connection:
        await connection.execute(
            "UPDATE notification.outbox SET lease_expires_at = now() - interval '1 second' WHERE id = %s",
            (claimed.id,),
        )
    assert await outbox_repository.claim(
        owner="crash-loop", limit=1, lease_seconds=30, max_attempts=1
    ) == ()
    stored = await outbox_repository.get(queued.id)
    assert stored.status == "failed"
    assert stored.attempts == 1
    assert stored.next_attempt_at is None


@pytest.mark.asyncio
async def test_fail_replay_requires_same_token_and_exact_effective_backoff(
    outbox_repository, trace_repository, handoff
):
    trace_id = await _trace(trace_repository)
    await outbox_repository.enqueue(
        tenant_id="tenant-a", customer_id="customer-a", trace_id=trace_id,
        idempotency_key=f"handoff:{trace_id}", event=handoff,
    )
    claimed = (await outbox_repository.claim(
        owner="worker", limit=1, lease_seconds=30, max_attempts=3
    ))[0]
    settled = await outbox_repository.fail(
        claimed.id, owner="worker", claim_token=claimed.claim_token,
        error_code="WEBHOOK_503", http_status=503, retryable=True,
        max_attempts=3, backoff_seconds=7,
    )
    assert settled.settlement_backoff_seconds == 7
    assert await outbox_repository.fail(
        claimed.id, owner="worker", claim_token=claimed.claim_token,
        error_code="WEBHOOK_503", http_status=503, retryable=True,
        max_attempts=3, backoff_seconds=7,
    ) == settled
    with pytest.raises(ValueError, match="settlement replay"):
        await outbox_repository.fail(
            claimed.id, owner="worker", claim_token=claimed.claim_token,
            error_code="WEBHOOK_503", http_status=503, retryable=True,
            max_attempts=3, backoff_seconds=9,
        )


@pytest.mark.asyncio
async def test_enqueue_atomically_finalizes_handoff_trace_for_identical_replay(
    outbox_repository, trace_repository, handoff
):
    trace_id = await _trace(trace_repository)
    span_id = await trace_repository.start_span(trace_id, "risk_precheck", tenant_id="tenant-a")
    failure = await trace_repository.append_event(
        trace_id=trace_id, span_id=span_id, tenant_id="tenant-a",
        event_type="risk_precheck.failed", component="risk_precheck", status="failed",
        error_code=handoff.reason_code, payload={"node": "risk_precheck"},
    )
    await outbox_repository.enqueue(
        tenant_id="tenant-a", customer_id="customer-a", trace_id=trace_id,
        idempotency_key=f"handoff:{trace_id}", event=handoff,
        primary_failure_event_id=failure.id, delivery_disposition="suppressed",
    )
    stored = await trace_repository.get_trace(trace_id, tenant_id="tenant-a")
    assert (stored.status, stored.terminal_outcome, stored.primary_failure_event_id) == (
        "failed", "handoff", failure.id
    )
    await trace_repository.finish_trace(
        trace_id, "failed", tenant_id="tenant-a",
        primary_failure_event_id=failure.id, terminal_outcome="handoff",
        delivery_disposition="suppressed",
    )


@pytest.mark.asyncio
async def test_invalid_atomic_finalization_rolls_back_outbox_insert(
    outbox_repository, trace_repository, handoff
):
    trace_id = await _trace(trace_repository)
    with pytest.raises(ValueError, match="primary failure event"):
        await outbox_repository.enqueue(
            tenant_id="tenant-a", customer_id="customer-a", trace_id=trace_id,
            idempotency_key=f"handoff:{trace_id}", event=handoff,
            primary_failure_event_id=9_999_999, delivery_disposition="suppressed",
        )
    assert await outbox_repository.count(tenant_id="tenant-a") == 0
    stored = await trace_repository.get_trace(trace_id, tenant_id="tenant-a")
    assert stored.status == "running"


@respx.mock
@pytest.mark.asyncio
async def test_webhook_signs_the_exact_deterministic_body(handoff):
    route = respx.post("https://hooks.example.test/handoff").mock(
        return_value=httpx.Response(202)
    )
    webhook = HandoffWebhook(
        url="https://hooks.example.test/handoff",
        secret="super-secret",
        client=httpx.AsyncClient(),
    )
    payload = {"trace_id": "trace-1", "event": handoff.model_dump(mode="json")}
    result = await webhook.deliver(
        payload, idempotency_key="handoff:trace-1", timestamp=1_700_000_000
    )

    request = route.calls[0].request
    expected_body = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    expected = hmac.new(
        b"super-secret", b"1700000000." + expected_body, hashlib.sha256
    ).hexdigest()
    assert request.content == expected_body
    assert request.headers["X-Agent-Timestamp"] == "1700000000"
    assert request.headers["X-Agent-Signature"] == f"sha256={expected}"
    assert request.headers["Idempotency-Key"] == "handoff:trace-1"
    assert result.http_status == 202
    await webhook.close()


@pytest.mark.parametrize(
    ("status", "retryable"),
    [(400, False), (408, True), (429, True), (500, True), (501, False), (502, True), (503, True), (504, True)],
)
@respx.mock
@pytest.mark.asyncio
async def test_webhook_classifies_http_failures_without_secret_material(
    handoff, status, retryable
):
    respx.post("https://hooks.example.test/handoff").mock(
        return_value=httpx.Response(status, text="bearer private-token super-secret")
    )
    webhook = HandoffWebhook(
        url="https://hooks.example.test/handoff", secret="super-secret",
        client=httpx.AsyncClient(),
    )
    with pytest.raises(WebhookDeliveryError) as caught:
        await webhook.deliver(handoff.model_dump(mode="json"), idempotency_key="key", timestamp=1)
    assert caught.value.retryable is retryable
    assert caught.value.http_status == status
    assert "super-secret" not in str(caught.value)
    assert "private-token" not in str(caught.value)
    await webhook.close()


@pytest.mark.parametrize("failure", [httpx.ConnectError("offline"), httpx.ReadTimeout("slow")])
@respx.mock
@pytest.mark.asyncio
async def test_webhook_classifies_network_failures_as_safe_retryable(handoff, failure):
    respx.post("https://hooks.example.test/handoff").mock(side_effect=failure)
    webhook = HandoffWebhook(
        url="https://hooks.example.test/handoff", secret="super-secret",
        client=httpx.AsyncClient(),
    )
    with pytest.raises(WebhookDeliveryError) as caught:
        await webhook.deliver(handoff.model_dump(mode="json"), idempotency_key="key", timestamp=1)
    assert caught.value.retryable is True
    assert caught.value.http_status is None
    assert caught.value.error_code in {"WEBHOOK_CONNECTION", "WEBHOOK_TIMEOUT"}
    assert "offline" not in str(caught.value)
    assert "slow" not in str(caught.value)
    await webhook.close()


class _RecordingRepository:
    def __init__(self, rows):
        self.rows = list(rows)
        self.completed = []
        self.failed = []

    async def claim(self, **kwargs):
        rows, self.rows = self.rows, []
        return tuple(rows)

    async def complete(self, row_id, **kwargs):
        self.completed.append((row_id, kwargs))

    async def fail(self, row_id, **kwargs):
        self.failed.append((row_id, kwargs))


class _Delivery:
    http_status = 202


@pytest.mark.asyncio
async def test_worker_bounds_attempts_and_persists_only_safe_failure_metadata(handoff):
    row = type("Row", (), {
        "id": uuid4(), "idempotency_key": "handoff:key", "attempts": 1,
        "claim_token": uuid4(),
        "payload": handoff.model_dump(mode="json")
    })()
    repository = _RecordingRepository([row])

    class FailingWebhook:
        async def deliver(self, *args, **kwargs):
            raise WebhookDeliveryError("WEBHOOK_429", retryable=True, http_status=429)

    worker = HandoffOutboxWorker(
        repository=repository, webhook=FailingWebhook(), owner="worker-a",
        batch_size=2, lease_seconds=30, max_attempts=3, base_backoff_seconds=2,
    )
    assert await worker.run_once() == 1
    assert repository.failed == [(row.id, {
        "owner": "worker-a", "claim_token": row.claim_token,
        "error_code": "WEBHOOK_429", "http_status": 429,
        "retryable": True, "max_attempts": 3, "backoff_seconds": 2,
    })]


@pytest.mark.asyncio
async def test_worker_caps_exponential_backoff(handoff):
    row = type("Row", (), {
        "id": uuid4(), "idempotency_key": "handoff:key", "attempts": 8,
        "claim_token": uuid4(),
        "payload": handoff.model_dump(mode="json")
    })()
    repository = _RecordingRepository([row])

    class FailingWebhook:
        async def deliver(self, *args, **kwargs):
            raise WebhookDeliveryError("WEBHOOK_503", retryable=True, http_status=503)

    worker = HandoffOutboxWorker(
        repository=repository, webhook=FailingWebhook(), owner="worker-a",
        batch_size=1, lease_seconds=30, max_attempts=10,
        base_backoff_seconds=2, max_backoff_seconds=30,
    )
    await worker.run_once()
    assert repository.failed[0][1]["backoff_seconds"] == 30


@pytest.mark.asyncio
async def test_worker_propagates_cancellation_and_leaves_claim_recoverable(handoff):
    row = type("Row", (), {
        "id": uuid4(), "idempotency_key": "handoff:key", "attempts": 1,
        "claim_token": uuid4(),
        "payload": handoff.model_dump(mode="json")
    })()
    repository = _RecordingRepository([row])
    entered = asyncio.Event()

    class BlockingWebhook:
        async def deliver(self, *args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

    worker = HandoffOutboxWorker(
        repository=repository, webhook=BlockingWebhook(), owner="worker-a",
        batch_size=1, lease_seconds=1, max_attempts=3, base_backoff_seconds=1,
    )
    task = asyncio.create_task(worker.run_once())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert repository.completed == []
    assert repository.failed == []


@pytest.mark.asyncio
async def test_worker_processes_claimed_batch_concurrently(handoff):
    rows = [type("Row", (), {
        "id": uuid4(), "idempotency_key": f"handoff:{index}", "attempts": 1,
        "claim_token": uuid4(), "payload": handoff.model_dump(mode="json"),
    })() for index in range(2)]
    repository = _RecordingRepository(rows)
    both_entered = asyncio.Event()
    release = asyncio.Event()
    entered = 0

    class CoordinatedWebhook:
        async def deliver(self, *args, **kwargs):
            nonlocal entered
            entered += 1
            if entered == 2:
                both_entered.set()
            await release.wait()
            return _Delivery()

    worker = HandoffOutboxWorker(
        repository=repository, webhook=CoordinatedWebhook(), owner="worker-a",
        batch_size=2, lease_seconds=30, max_attempts=3, base_backoff_seconds=1,
    )
    task = asyncio.create_task(worker.run_once())
    await asyncio.wait_for(both_entered.wait(), timeout=1)
    release.set()
    assert await task == 2
    assert {row_id for row_id, _ in repository.completed} == {row.id for row in rows}
    assert all(
        details["claim_token"] in {row.claim_token for row in rows}
        for _, details in repository.completed
    )
