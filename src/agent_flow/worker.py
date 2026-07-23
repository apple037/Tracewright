from __future__ import annotations

import asyncio

from agent_flow.adapters.webhook import HandoffWebhook, WebhookDeliveryError
from agent_flow.repositories.outbox import OutboxRepository


class HandoffOutboxWorker:
    def __init__(
        self,
        *,
        repository: OutboxRepository,
        webhook: HandoffWebhook,
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
                batch_size, lease_seconds, max_attempts,
                base_backoff_seconds, max_backoff_seconds,
            ) < 1
            or poll_seconds <= 0
        ):
            raise ValueError("worker bounds and owner must be positive")
        self.repository = repository
        self.webhook = webhook
        self.owner = owner
        self.batch_size = batch_size
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts
        self.base_backoff_seconds = base_backoff_seconds
        self.max_backoff_seconds = max_backoff_seconds
        self.poll_seconds = poll_seconds

    async def run_once(self) -> int:
        rows = await self.repository.claim(
            owner=self.owner, limit=self.batch_size, lease_seconds=self.lease_seconds
        )
        for row in rows:
            try:
                delivered = await self.webhook.deliver(
                    row.payload, idempotency_key=row.idempotency_key
                )
            except WebhookDeliveryError as error:
                await self.repository.fail(
                    row.id,
                    owner=self.owner,
                    error_code=error.error_code,
                    http_status=error.http_status,
                    retryable=error.retryable,
                    max_attempts=self.max_attempts,
                    backoff_seconds=min(
                        self.max_backoff_seconds,
                        self.base_backoff_seconds * 2 ** (row.attempts - 1),
                    ),
                )
            else:
                await self.repository.complete(
                    row.id, owner=self.owner, http_status=delivered.http_status
                )
        return len(rows)

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            processed = await self.run_once()
            if processed == 0:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self.poll_seconds)
                except TimeoutError:
                    pass
