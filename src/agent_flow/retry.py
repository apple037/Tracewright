from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal, TypeVar

from agent_flow.errors import AgentError


T = TypeVar("T")


@dataclass(frozen=True)
class AttemptRecord:
    attempt: int
    outcome: Literal["completed", "failed"]
    duration_ms: int
    backoff_ms: int
    error_code: str | None


@dataclass(frozen=True)
class CapacityWait:
    endpoint_name: str
    profile_name: str
    wait_ms: int
    wait_limit_kind: Literal["endpoint", "profile", "none"]


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 1
    base_delay_ms: int = 100
    max_delay_ms: int = 1000


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


async def run_with_retry(
    operation: Callable[[], Awaitable[T]], policy: RetryPolicy
) -> tuple[T, list[AttemptRecord]]:
    records: list[AttemptRecord] = []
    for attempt in range(1, policy.max_attempts + 1):
        started = time.monotonic()
        try:
            value = await operation()
            records.append(
                AttemptRecord(
                    attempt=attempt,
                    outcome="completed",
                    duration_ms=_elapsed_ms(started),
                    backoff_ms=0,
                    error_code=None,
                )
            )
            return value, records
        except AgentError as error:
            records.append(
                AttemptRecord(
                    attempt=attempt,
                    outcome="failed",
                    duration_ms=_elapsed_ms(started),
                    backoff_ms=0,
                    error_code=error.error_code,
                )
            )
            if not error.retryable or attempt == policy.max_attempts:
                raise
            delay_ms = min(
                policy.max_delay_ms,
                policy.base_delay_ms * 2 ** (attempt - 1),
            )
            await asyncio.sleep(delay_ms / 1000)
    raise RuntimeError("unreachable retry state")
