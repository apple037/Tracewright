from pathlib import Path

import pytest

from agent_flow.config import Settings, load_model_config
from agent_flow.model_registry import ModelInventoryProbe, ModelRegistry


@pytest.mark.live_model
@pytest.mark.asyncio
async def test_local_vllm_inventory_and_capabilities():
    registry = ModelRegistry(
        load_model_config(Path("config/models.bootstrap.example.yaml")),
        Settings(database_url="postgresql://unused:unused@invalid/live-probe"),
    )

    result = await ModelInventoryProbe(registry, timeout=20.0).probe_role(
        "response_generator"
    )

    assert result.model == "Qwen/Qwen3-8B-AWQ"
    assert registry.resolve("response_generator").min_context_length == 6144
    assert result.max_model_len == 6144
    assert result.verified_capabilities == frozenset(
        {"chat", "structured_json", "reasoning_toggle"}
    )
