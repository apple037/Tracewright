import asyncio

import pytest

from agent_flow.adapters.models import CapacityGuard
from agent_flow.config import EndpointConfig, ModelConfig, ProfileConfig


def _config(
    *, endpoint_limit: int, profile_limits: dict[str, int]
) -> ModelConfig:
    endpoint = EndpointConfig(
        adapter="openai_compatible",
        base_url_env="LOCAL_VLLM_BASE_URL",
        max_concurrency=endpoint_limit,
    )
    profiles = {
        name: ProfileConfig(
            endpoint="shared",
            model=f"test-{name}",
            family="test",
            capabilities={"chat"},
            max_concurrency=limit,
        )
        for name, limit in profile_limits.items()
    }
    # CapacityGuard must independently enforce both configured ceilings. Constructing
    # an otherwise impossible wider profile makes the endpoint invariant observable.
    return ModelConfig.model_construct(
        endpoints={"shared": endpoint},
        profiles=profiles,
        roles={},
        mode="bootstrap",
        disabled_roles=set(),
        promotion_semantic_mode="human_only",
    )


@pytest.mark.asyncio
async def test_endpoint_limit_wins_when_profile_limit_is_larger():
    guard = CapacityGuard(_config(endpoint_limit=1, profile_limits={"wide": 3}))
    entered = asyncio.Event()
    release = asyncio.Event()

    async def occupy_endpoint():
        async with guard.acquire("shared", "wide", timeout_ms=100):
            entered.set()
            await release.wait()

    holder = asyncio.create_task(occupy_endpoint())
    await entered.wait()
    try:
        with pytest.raises(TimeoutError):
            async with guard.acquire("shared", "wide", timeout_ms=10):
                pytest.fail("endpoint capacity was bypassed")
    finally:
        release.set()
        await holder


@pytest.mark.asyncio
async def test_wait_reports_the_semaphore_that_blocked_admission():
    endpoint_guard = CapacityGuard(
        _config(endpoint_limit=1, profile_limits={"wide": 3})
    )
    endpoint_release = asyncio.Event()
    endpoint_entered = asyncio.Event()

    async def hold_endpoint():
        async with endpoint_guard.acquire("shared", "wide", timeout_ms=100):
            endpoint_entered.set()
            await endpoint_release.wait()

    endpoint_holder = asyncio.create_task(hold_endpoint())
    await endpoint_entered.wait()
    endpoint_waiter = asyncio.create_task(
        _acquire_once(endpoint_guard, "wide", timeout_ms=100)
    )
    await asyncio.sleep(0)
    endpoint_release.set()
    endpoint_wait = await endpoint_waiter
    await endpoint_holder
    assert endpoint_wait.wait_limit_kind == "endpoint"

    profile_guard = CapacityGuard(
        _config(endpoint_limit=2, profile_limits={"narrow": 1})
    )
    profile_release = asyncio.Event()
    profile_entered = asyncio.Event()

    async def hold_profile():
        async with profile_guard.acquire("shared", "narrow", timeout_ms=100):
            profile_entered.set()
            await profile_release.wait()

    profile_holder = asyncio.create_task(hold_profile())
    await profile_entered.wait()
    profile_waiter = asyncio.create_task(
        _acquire_once(profile_guard, "narrow", timeout_ms=100)
    )
    await asyncio.sleep(0)
    profile_release.set()
    profile_wait = await profile_waiter
    await profile_holder
    assert profile_wait.wait_limit_kind == "profile"

    async with profile_guard.acquire(
        "shared", "narrow", timeout_ms=100
    ) as immediate_wait:
        assert immediate_wait.wait_limit_kind == "none"


@pytest.mark.asyncio
async def test_profile_timeout_releases_endpoint_permit():
    guard = CapacityGuard(
        _config(endpoint_limit=2, profile_limits={"blocked": 1, "other": 1})
    )
    release = asyncio.Event()
    entered = asyncio.Event()

    async def hold_profile():
        async with guard.acquire("shared", "blocked", timeout_ms=100):
            entered.set()
            await release.wait()

    holder = asyncio.create_task(hold_profile())
    await entered.wait()
    with pytest.raises(TimeoutError):
        async with guard.acquire("shared", "blocked", timeout_ms=10):
            pass

    try:
        async with guard.acquire("shared", "other", timeout_ms=50):
            pass
    finally:
        release.set()
        await holder


@pytest.mark.asyncio
async def test_cancellation_while_waiting_for_profile_releases_endpoint_permit():
    guard = CapacityGuard(
        _config(endpoint_limit=2, profile_limits={"blocked": 1, "other": 1})
    )
    release = asyncio.Event()
    entered = asyncio.Event()

    async def hold_profile():
        async with guard.acquire("shared", "blocked", timeout_ms=100):
            entered.set()
            await release.wait()

    holder = asyncio.create_task(hold_profile())
    await entered.wait()
    waiter = asyncio.create_task(_acquire_once(guard, "blocked", timeout_ms=1000))
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    try:
        async with guard.acquire("shared", "other", timeout_ms=50):
            pass
    finally:
        release.set()
        await holder


async def _acquire_once(
    guard: CapacityGuard, profile_name: str, *, timeout_ms: int
):
    async with guard.acquire(
        "shared", profile_name, timeout_ms=timeout_ms
    ) as wait:
        return wait
