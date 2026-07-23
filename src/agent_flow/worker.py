from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import socket

from agent_flow.adapters.webhook import HandoffWebhook, WebhookDeliveryError
from agent_flow.config import Settings
from agent_flow.logging import configure_json_stdout
from agent_flow.repositories.outbox import OutboxRepository
from agent_flow.repositories.postgres import PostgresPool
from agent_flow.repositories.retention import (
    PostgresRetentionRepository,
    RetentionResult,
)


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
            owner=self.owner, limit=self.batch_size,
            lease_seconds=self.lease_seconds, max_attempts=self.max_attempts,
        )
        async with asyncio.TaskGroup() as tasks:
            for row in rows:
                tasks.create_task(self._deliver(row))
        return len(rows)

    async def _deliver(self, row) -> None:
        try:
            delivered = await self.webhook.deliver(
                row.payload, idempotency_key=row.idempotency_key
            )
        except WebhookDeliveryError as error:
            await self.repository.fail(
                row.id,
                owner=self.owner,
                claim_token=row.claim_token,
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
                row.id, owner=self.owner, claim_token=row.claim_token,
                http_status=delivered.http_status,
            )

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            processed = await self.run_once()
            if processed == 0:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self.poll_seconds)
                except TimeoutError:
                    pass


class RetentionWorker:
    def __init__(
        self,
        repository: PostgresRetentionRepository,
        *,
        batch_size: int = 100,
        tenant_id: str | None = None,
    ) -> None:
        if batch_size < 1:
            raise ValueError("retention batch size must be positive")
        self.repository = repository
        self.batch_size = batch_size
        self.tenant_id = tenant_id

    async def run_once(self) -> RetentionResult:
        return await self.repository.cleanup_batch(
            limit=self.batch_size, tenant_id=self.tenant_id
        )


async def _run_retention_loop(
    worker: RetentionWorker,
    stop: asyncio.Event,
    *,
    interval_seconds: float = 60.0,
) -> None:
    while not stop.is_set():
        await worker.run_once()
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
        except TimeoutError:
            pass


async def run_worker_runtime(
    *,
    settings: Settings,
    stop: asyncio.Event,
    pool: PostgresPool | None = None,
    webhook: HandoffWebhook | None = None,
) -> None:
    runtime_pool = pool or PostgresPool(settings.database_url)
    runtime_webhook = webhook or HandoffWebhook(
        url=settings.webhook_url,
        secret=settings.webhook_secret,
    )
    opened = False
    try:
        await runtime_pool.open()
        opened = True
        outbox_worker = HandoffOutboxWorker(
            repository=OutboxRepository(runtime_pool),
            webhook=runtime_webhook,
            owner=os.getenv(
                "WORKER_OWNER",
                f"{socket.gethostname()}-{os.getpid()}",
            ),
        )
        retention_worker = RetentionWorker(PostgresRetentionRepository(runtime_pool))
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(outbox_worker.run(stop))
            tasks.create_task(_run_retention_loop(retention_worker, stop))
    finally:
        await runtime_webhook.close()
        if opened:
            await runtime_pool.close()


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Agent Flow background workers")
    parser.add_argument(
        "--run",
        action="store_true",
        help="run the handoff outbox and retention worker loops",
    )
    return parser


async def _run_cli() -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signal_name in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signal_name, stop.set)
        except (NotImplementedError, RuntimeError):
            pass
    await run_worker_runtime(settings=Settings(), stop=stop)


def main(argv: list[str] | None = None) -> None:
    arguments = build_argument_parser().parse_args(argv)
    if not arguments.run:
        build_argument_parser().error("the worker requires explicit --run")
    configure_json_stdout(logging.getLogger())
    asyncio.run(_run_cli())


if __name__ == "__main__":
    main()
