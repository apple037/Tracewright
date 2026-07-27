from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI

from agent_flow.adapters.evidence import MockRagClient, MockToolClient
from agent_flow.adapters.knowledge import KnowledgeSources
from agent_flow.adapters.models import ModelGateway
from agent_flow.api.dependencies import AppServices, Authenticator, ReadinessCheck
from agent_flow.api.config import router as config_router
from agent_flow.api.health import router as health_router
from agent_flow.api.sessions import router as sessions_router
from agent_flow.api.submissions import router as submissions_router
from agent_flow.api.traces import router as traces_router
from agent_flow.api.turns import router as turns_router
from agent_flow.auth import DemoTokenAuthenticator
from agent_flow.config import Settings, load_model_config
from agent_flow.inbound import InboundMessageService
from agent_flow.main import mount_console
from agent_flow.model_registry import ModelInventoryProbe, ModelRegistry
from agent_flow.observability import OperationTelemetry
from agent_flow.pipeline.turn import TurnPipeline
from agent_flow.repositories.conversations import PostgresConversationRepository
from agent_flow.repositories.outbox import OutboxRepository
from agent_flow.repositories.postgres import PostgresPool
from agent_flow.repositories.submissions import PostgresSubmissionRepository
from agent_flow.repositories.traces import PostgresTraceRepository
from agent_flow.runtime_config import RuntimeConfigService


_CONFIG_ROOT = Path("config")


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


@dataclass(frozen=True)
class RuntimeComponents:
    pool: PostgresPool
    traces: PostgresTraceRepository
    conversations: PostgresConversationRepository
    submissions: PostgresSubmissionRepository
    inbound: InboundMessageService
    pipeline: TurnPipeline
    authenticate: Authenticator
    inventory_probe: object
    runtime_config: RuntimeConfigService
    knowledge: object | None = None


async def build_demo_components(
    settings: Settings,
    pool: PostgresPool,
    *,
    inventory_probe: object | None = None,
) -> RuntimeComponents:
    if settings.app_runtime_mode != "demo":
        raise RuntimeError("unsupported runtime mode: only demo is implemented")
    config = load_model_config(settings.model_config_path)
    registry = ModelRegistry(config, settings)
    runtime_config = RuntimeConfigService(_CONFIG_ROOT, config, settings)
    traces = PostgresTraceRepository(pool)
    telemetry = OperationTelemetry(traces)
    models = ModelGateway(registry, telemetry=telemetry, timeout=90.0)
    rag = (
        KnowledgeSources.from_config(
            settings.knowledge_config_path, telemetry=telemetry
        )
        if settings.knowledge_config_path.exists()
        else MockRagClient.from_fixture(
            settings.demo_rag_fixture, telemetry=telemetry
        )
    )
    tools = MockToolClient.from_fixture(settings.demo_tool_fixture, telemetry=telemetry)
    conversations = PostgresConversationRepository(
        pool, history_turns=settings.history_turns
    )
    handoffs = OutboxRepository(pool)
    pipeline = TurnPipeline(
        traces=traces,
        conversations=conversations,
        handoffs=handoffs,
        models=models,
        rag=rag,
        tools=tools,
        artifacts=runtime_config.artifacts,
        clock=SystemClock(),
        assurance_mode=settings.assurance_mode,
        telemetry=telemetry,
    )
    submissions = PostgresSubmissionRepository(pool)
    inbound = InboundMessageService(submissions)
    return RuntimeComponents(
        pool=pool,
        traces=traces,
        conversations=conversations,
        submissions=submissions,
        inbound=inbound,
        pipeline=pipeline,
        authenticate=DemoTokenAuthenticator.from_settings(settings),
        inventory_probe=inventory_probe or ModelInventoryProbe(registry, timeout=90.0),
        runtime_config=runtime_config,
        knowledge=rag if isinstance(rag, KnowledgeSources) else None,
    )


async def _probe_models_check(components: RuntimeComponents) -> ReadinessCheck:
    try:
        await components.inventory_probe.probe_all()
    except RuntimeError:
        # Never surface model output or exception bodies in readiness details.
        return ReadinessCheck(
            status="unavailable", stage="capability",
            error_code="MODEL_CAPABILITY_FAILED",
        )
    return ReadinessCheck(status="ok")


def _install_services(
    app: FastAPI, components: RuntimeComponents, models_check: ReadinessCheck,
    settings: Settings,
) -> None:
    app.state.services = AppServices(
        pipeline=components.pipeline,
        traces=components.traces,
        conversations=components.conversations,
        authenticate=components.authenticate,
        artifact_root=_CONFIG_ROOT,
        artifact_status="ok",
        dependency_checks={
            "database": ReadinessCheck(status="ok"),
            "models": models_check,
        },
        submissions=components.submissions,
        inbound=components.inbound,
        legacy_turn_timeout_seconds=settings.legacy_turn_timeout_seconds,
        runtime_config=components.runtime_config,
        knowledge=components.knowledge,
        recheck_models=lambda: _probe_models_check(components),
    )


def create_runtime_app(
    *,
    settings: Settings | None = None,
    pool: PostgresPool | None = None,
    inventory_probe: object | None = None,
) -> FastAPI:
    resolved_settings = settings or Settings()

    @asynccontextmanager
    async def demo_lifespan(app: FastAPI):
        runtime_pool = pool or PostgresPool(resolved_settings.database_url)
        await runtime_pool.open()
        try:
            components = await build_demo_components(
                resolved_settings, runtime_pool, inventory_probe=inventory_probe
            )
            models_check = await _probe_models_check(components)
            _install_services(app, components, models_check, resolved_settings)
            yield
        finally:
            await runtime_pool.close()

    app = FastAPI(title="Agent Flow", version="0.1.0", lifespan=demo_lifespan)
    app.include_router(turns_router)
    app.include_router(submissions_router)
    app.include_router(sessions_router)
    app.include_router(traces_router)
    app.include_router(config_router)
    app.include_router(health_router)
    mount_console(app)
    return app
