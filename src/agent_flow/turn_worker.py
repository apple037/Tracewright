from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import Any, TypeVar
from uuid import UUID

from psycopg import OperationalError
from pydantic import ValidationError

from agent_flow.auth import AuthorizedCustomerContext
from agent_flow.contracts import InboundMessage, SubmissionResult, TurnResult
from agent_flow.errors import AgentError
from agent_flow.pipeline.turn import TurnPipeline
from agent_flow.repositories.submissions import (
    PostgresSubmissionRepository,
    SubmissionRecord,
)


T = TypeVar("T")
_RETRY_FIELDS = {
    "retry_of_trace_id",
    "retry_initiator",
    "retry_reason",
    "delivery_disposition",
}


class _InvalidSubmission(Exception):
    pass


class _AuthorizationScopeCorruption(Exception):
    pass


class _LeaseLost(Exception):
    pass


class TurnJobWorker:
    def __init__(
        self,
        *,
        repository: PostgresSubmissionRepository,
        pipeline: TurnPipeline,
        owner: str,
        batch_size: int = 10,
        lease_seconds: int = 30,
        max_attempts: int = 3,
        base_backoff_seconds: int = 2,
        max_backoff_seconds: int = 60,
        poll_seconds: float = 1.0,
    ) -> None:
        if (
            not owner
            or min(
                batch_size,
                lease_seconds,
                max_attempts,
                base_backoff_seconds,
                max_backoff_seconds,
            )
            < 1
            or poll_seconds <= 0
        ):
            raise ValueError("worker bounds and owner must be positive")
        self.repository = repository
        self.pipeline = pipeline
        self.owner = owner
        self.batch_size = batch_size
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts
        self.base_backoff_seconds = base_backoff_seconds
        self.max_backoff_seconds = max_backoff_seconds
        self.poll_seconds = poll_seconds

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

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                processed = await self.run_once()
            except (TimeoutError, OSError, OperationalError):
                processed = 0
            if processed == 0:
                try:
                    await asyncio.wait_for(
                        stop.wait(), timeout=self.poll_seconds
                    )
                except TimeoutError:
                    pass

    async def _execute(self, record: SubmissionRecord) -> None:
        try:
            record, recovery_retry_of = await self._recover_if_needed(record)
        except asyncio.CancelledError:
            raise
        except (TimeoutError, OSError, OperationalError):
            try:
                await self._fail(
                    record,
                    error_code="DEPENDENCY_TIMEOUT",
                    error_component="turn_worker",
                    retryable=True,
                )
            except Exception:
                pass
            return
        except Exception:
            try:
                await self._fail(
                    record,
                    error_code="UNEXPECTED_ERROR",
                    error_component="turn_worker",
                    retryable=False,
                )
            except Exception:
                pass
            return

        try:
            await self._with_heartbeat(
                record,
                self._process_claim(record, recovery_retry_of),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            # Ownership loss and settlement failures are isolated to this
            # claim. The repository lease decides whether it can be retried.
            return

    async def _process_claim(
        self,
        record: SubmissionRecord,
        recovery_retry_of: UUID | None,
    ) -> None:
        try:
            context, message, retry = self._decode(record)
            result = await self.pipeline.run(
                context,
                message.to_turn_request(),
                retry_of=(
                    recovery_retry_of
                    if recovery_retry_of is not None
                    else retry["retry_of_trace_id"]
                ),
                trace_id=record.trace_id,
                retry_initiator=retry["retry_initiator"],
                retry_reason=retry["retry_reason"],
                delivery_disposition=retry["delivery_disposition"],
                suppress_handoff=(
                    retry["delivery_disposition"] == "review_required"
                ),
                max_retry_count=(
                    self.max_attempts
                    if (
                        recovery_retry_of is not None
                        or retry["retry_of_trace_id"] is not None
                    )
                    else None
                ),
                finalize_on_cancellation=False,
            )
        except _AuthorizationScopeCorruption:
            await self._fail(
                record,
                error_code="AUTH_SCOPE_CORRUPTION",
                error_component="turn_worker",
                retryable=False,
            )
            return
        except _InvalidSubmission:
            await self._fail(
                record,
                error_code="INVALID_PAYLOAD",
                error_component="turn_worker",
                retryable=False,
            )
            return
        except AgentError as error:
            await self._fail(
                record,
                error_code=error.error_code,
                error_component=(
                    error.component
                    or error.failure_stage
                    or "turn_pipeline"
                ),
                retryable=error.retryable,
            )
            return
        except (TimeoutError, OSError, OperationalError):
            await self._fail(
                record,
                error_code="DEPENDENCY_TIMEOUT",
                error_component="turn_worker",
                retryable=True,
            )
            return
        except Exception:
            await self._fail(
                record,
                error_code="UNEXPECTED_ERROR",
                error_component="turn_worker",
                retryable=False,
            )
            return

        await self.repository.complete(
            record.id,
            owner=self.owner,
            claim_token=record.claim_token,
            result=self._submission_result(record, result),
        )

    async def _recover_if_needed(
        self, record: SubmissionRecord
    ) -> tuple[SubmissionRecord, UUID | None]:
        if record.attempts <= 1:
            return record, None
        original_trace_id = record.trace_id
        recovered = await self.repository.recover_expired_claim(
            record.id,
            owner=self.owner,
            claim_token=record.claim_token,
            error_code="WORKER_LEASE_EXPIRED",
        )
        return (
            recovered,
            (
                original_trace_id
                if recovered.trace_id != original_trace_id
                else None
            ),
        )

    def _decode(
        self, record: SubmissionRecord
    ) -> tuple[
        AuthorizedCustomerContext,
        InboundMessage,
        dict[str, Any],
    ]:
        if not all(
            isinstance(value, str) and value.strip() and len(value) <= 256
            for value in (record.tenant_id, record.customer_id)
        ):
            raise _AuthorizationScopeCorruption
        if (
            not isinstance(record.payload, dict)
            or set(record.payload) != {"message", "retry"}
        ):
            raise _InvalidSubmission
        retry_payload = record.payload["retry"]
        if (
            not isinstance(retry_payload, dict)
            or set(retry_payload) != _RETRY_FIELDS
        ):
            raise _InvalidSubmission
        try:
            message = InboundMessage.model_validate(record.payload["message"])
            retry_of_value = retry_payload["retry_of_trace_id"]
            retry_of = (
                UUID(retry_of_value)
                if retry_of_value is not None
                else None
            )
        except (ValidationError, TypeError, ValueError):
            raise _InvalidSubmission from None
        for field in (
            "retry_initiator",
            "retry_reason",
            "delivery_disposition",
        ):
            if (
                retry_payload[field] is not None
                and not isinstance(retry_payload[field], str)
            ):
                raise _InvalidSubmission
        if retry_payload["delivery_disposition"] not in {
            None,
            "deliver",
            "review_required",
            "suppressed",
        }:
            raise _InvalidSubmission
        return (
            AuthorizedCustomerContext(
                subject_id=f"turn-worker:{self.owner}",
                tenant_id=record.tenant_id,
                customer_id=record.customer_id,
            ),
            message,
            {
                **retry_payload,
                "retry_of_trace_id": retry_of,
            },
        )

    async def _with_heartbeat(
        self,
        record: SubmissionRecord,
        operation: Awaitable[T],
    ) -> T:
        done = asyncio.Event()

        async def run_operation() -> T:
            try:
                return await operation
            finally:
                done.set()

        async def heartbeat() -> None:
            interval = self.lease_seconds / 3
            while not done.is_set():
                try:
                    await asyncio.wait_for(done.wait(), timeout=interval)
                except TimeoutError:
                    active = await self.repository.heartbeat(
                        record.id,
                        owner=self.owner,
                        claim_token=record.claim_token,
                        lease_seconds=self.lease_seconds,
                    )
                    if not active:
                        raise _LeaseLost

        operation_task = asyncio.create_task(run_operation())
        heartbeat_task = asyncio.create_task(heartbeat())
        try:
            finished, _ = await asyncio.wait(
                (operation_task, heartbeat_task),
                return_when=asyncio.FIRST_EXCEPTION,
            )
            if heartbeat_task in finished:
                heartbeat_error = heartbeat_task.exception()
                if heartbeat_error is not None:
                    operation_task.cancel()
                    await asyncio.gather(
                        operation_task, return_exceptions=True
                    )
                    raise heartbeat_error
            result = await operation_task
            done.set()
            await heartbeat_task
            return result
        finally:
            done.set()
            for task in (operation_task, heartbeat_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(
                operation_task, heartbeat_task, return_exceptions=True
            )

    async def _fail(
        self,
        record: SubmissionRecord,
        *,
        error_code: str,
        error_component: str,
        retryable: bool,
    ) -> None:
        await self.repository.fail(
            record.id,
            owner=self.owner,
            claim_token=record.claim_token,
            error_code=error_code,
            error_component=error_component,
            retryable=retryable,
            max_attempts=self.max_attempts,
            backoff_seconds=min(
                self.max_backoff_seconds,
                self.base_backoff_seconds
                * 2 ** max(0, record.attempts - 1),
            ),
        )

    @staticmethod
    def _submission_result(
        record: SubmissionRecord, result: TurnResult
    ) -> SubmissionResult:
        return SubmissionResult(
            submission_id=record.id,
            trace_id=result.trace_id,
            status="completed",
            text=result.text,
            citations=result.citations,
            handoff=result.handoff,
        )
