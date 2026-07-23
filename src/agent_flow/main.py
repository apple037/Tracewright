from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI

from agent_flow.api.dependencies import AppServices, ReadinessCheck
from agent_flow.api.health import router as health_router
from agent_flow.api.submissions import router as submissions_router
from agent_flow.api.traces import router as traces_router
from agent_flow.api.turns import router as turns_router
from agent_flow.artifacts import load_runtime_artifacts


_READINESS_CHECKS = ("database", "models")
_READINESS_STATUSES = frozenset({"ok", "missing", "invalid", "unavailable"})


def _normalize_check(value: object) -> ReadinessCheck:
    if isinstance(value, ReadinessCheck):
        return value
    if isinstance(value, str) and value in _READINESS_STATUSES:
        return ReadinessCheck(status=value)
    return ReadinessCheck(status="unavailable")


def _safe_dependency_checks(
    values: dict[str, object] | None,
) -> dict[str, ReadinessCheck]:
    checks = {key: ReadinessCheck(status="unavailable") for key in _READINESS_CHECKS}
    for key, value in (values or {}).items():
        if key in checks:
            checks[key] = _normalize_check(value)
    return checks


def create_app(
    *, pipeline=None, traces=None, conversations=None, authenticate=None,
    artifact_root: Path = Path("config"),
    dependency_checks: dict[str, object] | None = None,
    submissions=None, inbound=None,
    legacy_turn_timeout_seconds: float = 60.0,
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
        dependency_checks=_safe_dependency_checks(dependency_checks),
        submissions=submissions, inbound=inbound,
        legacy_turn_timeout_seconds=legacy_turn_timeout_seconds,
    )
    app.include_router(turns_router)
    app.include_router(submissions_router)
    app.include_router(traces_router)
    app.include_router(health_router)
    return app


app = create_app()
