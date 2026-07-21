import asyncio

import httpx
import pytest
import respx

from agent_flow.adapters.models import CapacityGuard, EmbeddingModel, ModelGateway
from agent_flow.config import ModelConfig, Settings
from agent_flow.contracts import ResponseDraft
from agent_flow.model_registry import ModelRegistry


def _config(
    *, endpoint_limit: int, profile_limits: dict[str, int]
) -> ModelConfig:
    return ModelConfig.model_validate(
        {
            "endpoints": {
                "shared": {
                    "adapter": "openai_compatible",
                    "base_url_env": "LOCAL_VLLM_BASE_URL",
                    "max_concurrency": endpoint_limit,
                }
            },
            "profiles": {
                name: {
                    "endpoint": "shared",
                    "model": f"test-{name}",
                    "family": "test",
                    "capabilities": ["chat"],
                    "max_concurrency": limit,
                }
                for name, limit in profile_limits.items()
            },
            "roles": {},
            "mode": "bootstrap",
            "disabled_roles": [],
            "promotion_semantic_mode": "human_only",
        }
    )


def _gateway_registry(*, endpoint_limit: int, profile_limit: int) -> ModelRegistry:
    config = ModelConfig.model_validate(
        {
            "endpoints": {
                "remote": {
                    "adapter": "ollama_compatible",
                    "base_url_env": "REMOTE_MODEL_BASE_URL",
                    "max_concurrency": endpoint_limit,
                }
            },
            "profiles": {
                "chat": {
                    "endpoint": "remote",
                    "model": "chat-model",
                    "family": "test",
                    "capabilities": ["chat", "structured_json"],
                    "max_concurrency": profile_limit,
                },
                "embedding": {
                    "endpoint": "remote",
                    "model": "embedding-model",
                    "family": "test",
                    "capabilities": ["embedding"],
                    "max_concurrency": profile_limit,
                },
            },
            "roles": {"chat": "chat", "embedding": "embedding"},
            "mode": "bootstrap",
            "disabled_roles": [],
            "promotion_semantic_mode": "human_only",
        }
    )
    return ModelRegistry(
        config,
        Settings(
            database_url="postgresql://agent:agent@localhost/agent",
            remote_model_base_url="http://remote-models:11434",
        ),
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
@respx.mock
async def test_gateway_and_embedding_instances_share_endpoint_capacity():
    registry = _gateway_registry(endpoint_limit=1, profile_limit=1)
    chat_started = asyncio.Event()
    release_chat = asyncio.Event()

    async def block_chat(request):
        chat_started.set()
        await release_chat.wait()
        return httpx.Response(200, json={"message": {"content": "ok"}})

    chat_route = respx.post("http://remote-models:11434/api/chat").mock(
        side_effect=block_chat
    )
    embed_route = respx.post("http://remote-models:11434/api/embed").mock(
        return_value=httpx.Response(200, json={"embeddings": [[0.1]]})
    )
    chat_task = asyncio.create_task(
        ModelGateway(registry).complete("chat", {"messages": []})
    )
    await chat_started.wait()
    try:
        with pytest.raises(TimeoutError):
            await EmbeddingModel(registry, acquire_timeout_ms=10).embed(
                "embedding", ["hello"]
            )
        assert not embed_route.called
    finally:
        release_chat.set()
        assert await chat_task == "ok"
    assert chat_route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_gateway_instances_share_profile_capacity_for_structured_and_chat():
    registry = _gateway_registry(endpoint_limit=2, profile_limit=1)
    structured_started = asyncio.Event()
    release_structured = asyncio.Event()

    async def block_structured(request):
        structured_started.set()
        await release_structured.wait()
        return httpx.Response(
            200,
            json={
                "message": {
                    "content": '{"text":"ok","citations":[],"evidence_ids":[]}'
                }
            },
        )

    chat_route = respx.post("http://remote-models:11434/api/chat").mock(
        side_effect=block_structured
    )
    structured_task = asyncio.create_task(
        ModelGateway(registry).structured(
            "chat", {"messages": []}, ResponseDraft
        )
    )
    await structured_started.wait()
    try:
        with pytest.raises(TimeoutError):
            await ModelGateway(registry, acquire_timeout_ms=10).complete(
                "chat", {"messages": []}
            )
    finally:
        release_structured.set()
        assert (await structured_task).text == "ok"
    assert chat_route.call_count == 1


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
