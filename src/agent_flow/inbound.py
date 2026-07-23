from __future__ import annotations

import asyncio
from uuid import UUID

from agent_flow.auth import AuthorizedCustomerContext
from agent_flow.contracts import InboundMessage, SubmissionReceipt, SubmissionResult


class InboundMessageService:
    """Channel-neutral submission orchestration over the persistent queue."""

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
