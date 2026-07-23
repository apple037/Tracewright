from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI

from agent_flow.api.dependencies import AppServices
from agent_flow.api.health import router as health_router
from agent_flow.api.traces import router as traces_router
from agent_flow.api.turns import router as turns_router
from agent_flow.artifacts import load_runtime_artifacts


def create_app(
    *, pipeline=None, traces=None, conversations=None, authenticate=None,
    artifact_root: Path = Path("config"),
    dependency_checks: dict[str, str] | None = None,
) -> FastAPI:
    if not artifact_root.exists():
        artifact_status = "missing"
    else:
        try:
            load_runtime_artifacts(artifact_root)
        except FileNotFoundError:
            artifact_status = "missing"
        except Exception:
            artifact_status = "invalid"
        else:
            artifact_status = "ok"
    app = FastAPI(title="Agent Flow", version="0.1.0")
    app.state.services = AppServices(
        pipeline=pipeline, traces=traces, conversations=conversations,
        authenticate=authenticate, artifact_root=artifact_root,
        artifact_status=artifact_status,
        dependency_checks=dict(dependency_checks or {}),
    )
    app.include_router(turns_router)
    app.include_router(traces_router)
    app.include_router(health_router)
    return app


app = create_app()
