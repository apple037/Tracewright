import asyncio
from pathlib import Path

import pytest

from agent_flow.artifacts import load_runtime_artifacts
from agent_flow.config import Settings, load_model_config
from agent_flow.main import create_app
from agent_flow.model_registry import ModelRegistry
from agent_flow.worker import RetentionWorker


def test_bootstrap_components_load_offline_without_external_calls(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("ordinary bootstrap acceptance must stay offline")

    monkeypatch.setattr("httpx.Client", forbidden)
    monkeypatch.setattr("httpx.AsyncClient", forbidden)
    config = load_model_config(Path("config/models.bootstrap.example.yaml"))
    settings = Settings(
        database_url="postgresql://unused:unused@invalid/offline",
        local_vllm_base_url="http://localhost:8000/v1",
    )

    registry = ModelRegistry(config, settings)
    artifacts = load_runtime_artifacts(Path("config"))
    app = create_app(
        artifact_root=Path("config"),
        dependency_checks={"database": "unavailable", "models": "unavailable"},
    )

    assert registry.resolve("response_generator").model == "Qwen/Qwen3-8B-AWQ"
    assert artifacts.strategy_prompt.ref.version
    assert app.state.services.artifact_status == "ok"
    assert app.state.services.dependency_checks == {
        "database": "unavailable",
        "models": "unavailable",
    }


@pytest.mark.asyncio
async def test_retention_worker_propagates_cancellation():
    started = asyncio.Event()

    class BlockingRepository:
        async def cleanup_batch(self, *, limit, tenant_id):
            started.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(
        RetentionWorker(BlockingRepository(), batch_size=1).run_once()
    )
    await started.wait()

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
