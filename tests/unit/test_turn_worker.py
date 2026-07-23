import asyncio
import json
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_flow.contracts import (
    AssuranceMetadata,
    InboundMessage,
    TurnResult,
)
from agent_flow.errors import AgentError
from agent_flow.turn_worker import TurnJobWorker


def _message_payload():
    return InboundMessage(
        channel="web",
        external_message_id="message-1",
        session_id="session-1",
        text="Where is my order?",
        idempotency_key="message-1",
        metadata={"source": "portal"},
    ).model_dump(mode="json")


@pytest.fixture
def queued_submission():
    return SimpleNamespace(
        id=uuid4(),
        trace_id=uuid4(),
        tenant_id="tenant-1",
        customer_id="customer-1",
        status="running",
        attempts=1,
        payload={
            "message": _message_payload(),
            "retry": {
                "retry_of_trace_id": None,
                "retry_initiator": None,
                "retry_reason": None,
                "delivery_disposition": None,
            },
        },
        result=None,
        last_error_code=None,
        last_error_component=None,
        lease_expires_at=datetime.now(timezone.utc),
        claim_token=uuid4(),
        created_at=datetime.now(timezone.utc),
        finished_at=None,
    )


class FakeSubmissionRepository:
    def __init__(self, record):
        self.records = [record]
        self.claimed = False
        self.heartbeat_calls = 0
        self.original_trace_id = record.trace_id
        self.abandoned_error_code = None
        self.retry_of_trace_id = None

    async def claim(self, **_options):
        if self.claimed:
            return ()
        self.claimed = True
        return tuple(self.records)

    async def heartbeat(self, *_args, **_options):
        self.heartbeat_calls += 1
        return True

    async def complete(self, submission_id, *, result, **_options):
        record = next(row for row in self.records if row.id == submission_id)
        record.status = "completed"
        record.result = result.model_dump(mode="json")
        return True

    async def fail(
        self,
        submission_id,
        *,
        error_code,
        error_component,
        retryable,
        max_attempts,
        **_options,
    ):
        record = next(row for row in self.records if row.id == submission_id)
        record.status = (
            "queued" if retryable and record.attempts < max_attempts else "failed"
        )
        record.last_error_code = error_code
        record.last_error_component = error_component
        record.result = None
        return True

    async def recover_expired_claim(
        self, submission_id, *, error_code, **_options
    ):
        record = next(row for row in self.records if row.id == submission_id)
        self.abandoned_error_code = error_code
        old_trace_id = record.trace_id
        record.trace_id = uuid4()
        self.retry_of_trace_id = old_trace_id
        return record

    async def get_unscoped(self, submission_id):
        return next(row for row in self.records if row.id == submission_id)


@pytest.fixture
def fake_submission_repository(queued_submission):
    return FakeSubmissionRepository(queued_submission)


class SuccessfulPipeline:
    def __init__(self):
        self.calls = []

    async def run(self, context, request, **options):
        self.calls.append((context, request, options))
        return TurnResult(
            trace_id=options["trace_id"],
            text="Your order is in transit.",
            citations=("tool-result-1",),
            assurance=AssuranceMetadata(
                mode="reduced_assurance", judges=("response_judge",)
            ),
        )


@pytest.fixture
def pipeline():
    return SuccessfulPipeline()


@pytest.mark.asyncio
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
    context, request, options = pipeline.calls[0]
    assert (context.tenant_id, context.customer_id) == (
        queued_submission.tenant_id,
        queued_submission.customer_id,
    )
    assert request == InboundMessage.model_validate(
        queued_submission.payload["message"]
    ).to_turn_request()
    assert options["trace_id"] == queued_submission.trace_id
    assert options["finalize_on_cancellation"] is False


@pytest.mark.asyncio
async def test_worker_failure_records_location_without_exception_body(
    fake_submission_repository,
):
    class FailingPipeline:
        async def run(self, *_args, **_options):
            raise AgentError.dependency(
                "MODEL_TIMEOUT",
                failure_stage="response_generator",
                component="response_generator",
                retryable=False,
                public_message="private transport body",
            )

    worker = TurnJobWorker(
        repository=fake_submission_repository,
        pipeline=FailingPipeline(),
        owner="worker-1",
    )

    await worker.run_once()

    stored = fake_submission_repository.records[0]
    assert stored.last_error_code == "MODEL_TIMEOUT"
    assert stored.last_error_component == "response_generator"
    assert "private transport body" not in json.dumps(stored.result or {})


@pytest.mark.asyncio
async def test_worker_heartbeats_while_pipeline_is_running(
    fake_submission_repository,
):
    class BlockingPipeline:
        def __init__(self):
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def run(self, *_args, **_options):
            self.started.set()
            await self.release.wait()
            return TurnResult(
                trace_id=_options["trace_id"],
                text="done",
                assurance=AssuranceMetadata(
                    mode="reduced_assurance", judges=("response_judge",)
                ),
            )

    pipeline = BlockingPipeline()
    worker = TurnJobWorker(
        repository=fake_submission_repository,
        pipeline=pipeline,
        owner="worker-1",
        lease_seconds=2,
    )

    task = asyncio.create_task(worker.run_once())
    await pipeline.started.wait()
    await asyncio.sleep(1.1)

    assert fake_submission_repository.heartbeat_calls >= 1
    pipeline.release.set()
    assert await task == 1


@pytest.mark.asyncio
async def test_reclaimed_active_trace_creates_retry_lineage(
    fake_submission_repository, pipeline
):
    fake_submission_repository.records[0].attempts = 2
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
    assert (
        pipeline.calls[0][2]["retry_of"]
        == fake_submission_repository.original_trace_id
    )


@pytest.mark.asyncio
async def test_worker_uses_stored_retry_controls_not_channel_metadata(
    fake_submission_repository, pipeline
):
    record = fake_submission_repository.records[0]
    retry_of = uuid4()
    record.payload["retry"] = {
        "retry_of_trace_id": str(retry_of),
        "retry_initiator": "operator-1",
        "retry_reason": "verified transient failure",
        "delivery_disposition": "review_required",
    }

    await TurnJobWorker(
        repository=fake_submission_repository,
        pipeline=pipeline,
        owner="worker-1",
    ).run_once()

    options = pipeline.calls[0][2]
    assert options["retry_of"] == retry_of
    assert options["retry_initiator"] == "operator-1"
    assert options["retry_reason"] == "verified transient failure"
    assert options["delivery_disposition"] == "review_required"
    assert options["suppress_handoff"] is True


@pytest.mark.asyncio
async def test_invalid_payload_is_terminal_and_never_reaches_pipeline(
    fake_submission_repository, pipeline
):
    fake_submission_repository.records[0].payload["message"]["text"] = ""

    await TurnJobWorker(
        repository=fake_submission_repository,
        pipeline=pipeline,
        owner="worker-1",
    ).run_once()

    stored = fake_submission_repository.records[0]
    assert stored.status == "failed"
    assert stored.last_error_code == "INVALID_PAYLOAD"
    assert stored.last_error_component == "turn_worker"
    assert pipeline.calls == []


@pytest.mark.asyncio
async def test_corrupt_authorization_scope_is_terminal(
    fake_submission_repository, pipeline
):
    fake_submission_repository.records[0].tenant_id = ""

    await TurnJobWorker(
        repository=fake_submission_repository,
        pipeline=pipeline,
        owner="worker-1",
    ).run_once()

    stored = fake_submission_repository.records[0]
    assert stored.status == "failed"
    assert stored.last_error_code == "AUTH_SCOPE_CORRUPTION"
    assert stored.last_error_component == "turn_worker"
    assert pipeline.calls == []


@pytest.mark.asyncio
async def test_worker_heartbeats_through_submission_settlement(
    queued_submission, pipeline
):
    class BlockingCompleteRepository(FakeSubmissionRepository):
        def __init__(self, record):
            super().__init__(record)
            self.complete_started = asyncio.Event()
            self.release_complete = asyncio.Event()

        async def complete(self, *args, **kwargs):
            self.complete_started.set()
            await self.release_complete.wait()
            return await super().complete(*args, **kwargs)

    repository = BlockingCompleteRepository(queued_submission)
    worker = TurnJobWorker(
        repository=repository,
        pipeline=pipeline,
        owner="worker-1",
        lease_seconds=2,
    )

    task = asyncio.create_task(worker.run_once())
    await repository.complete_started.wait()
    await asyncio.sleep(1.1)

    assert repository.heartbeat_calls >= 1
    repository.release_complete.set()
    assert await task == 1


@pytest.mark.asyncio
async def test_settlement_failure_is_isolated_to_its_claim(
    queued_submission, pipeline
):
    second = deepcopy(queued_submission)
    second.id = uuid4()
    second.trace_id = uuid4()
    second.claim_token = uuid4()

    class PartiallyFailingRepository(FakeSubmissionRepository):
        def __init__(self, records):
            super().__init__(records[0])
            self.records = records

        async def complete(self, submission_id, **options):
            if submission_id == self.records[0].id:
                raise ValueError("claim ownership changed")
            return await super().complete(submission_id, **options)

    repository = PartiallyFailingRepository([queued_submission, second])

    assert await TurnJobWorker(
        repository=repository,
        pipeline=pipeline,
        owner="worker-1",
    ).run_once() == 2
    assert second.status == "completed"


@pytest.mark.asyncio
async def test_psycopg_operational_error_is_retryable(
    fake_submission_repository,
):
    from psycopg import OperationalError

    class FailingPipeline:
        async def run(self, *_args, **_options):
            raise OperationalError("private database address")

    await TurnJobWorker(
        repository=fake_submission_repository,
        pipeline=FailingPipeline(),
        owner="worker-1",
    ).run_once()

    stored = fake_submission_repository.records[0]
    assert stored.status == "queued"
    assert stored.last_error_code == "DEPENDENCY_TIMEOUT"
    assert "private database address" not in json.dumps(stored.result or {})


@pytest.mark.asyncio
async def test_worker_loop_survives_transient_claim_failure(pipeline):
    from psycopg import OperationalError

    stop = asyncio.Event()

    class TransientClaimRepository:
        def __init__(self):
            self.calls = 0

        async def claim(self, **_options):
            self.calls += 1
            if self.calls == 1:
                raise OperationalError("database unavailable")
            stop.set()
            return ()

    repository = TransientClaimRepository()
    await TurnJobWorker(
        repository=repository,
        pipeline=pipeline,
        owner="worker-1",
        poll_seconds=0.01,
    ).run(stop)

    assert repository.calls == 2


def test_worker_runtime_runs_constructed_turn_worker():
    from agent_flow.worker import run_worker_runtime

    class Pool:
        async def open(self):
            pass

        async def close(self):
            pass

    class Webhook:
        async def close(self):
            pass

    class ConstructedWorker:
        def __init__(self):
            self.stops = []

        async def run(self, stop):
            self.stops.append(stop)

    stop = asyncio.Event()
    stop.set()
    turn_worker = ConstructedWorker()

    asyncio.run(
        run_worker_runtime(
            settings=SimpleNamespace(
                webhook_url="http://example.invalid",
                webhook_secret="not-used",
            ),
            stop=stop,
            pool=Pool(),
            webhook=Webhook(),
            turn_worker=turn_worker,
        )
    )

    assert turn_worker.stops == [stop]
