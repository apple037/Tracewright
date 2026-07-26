from __future__ import annotations

import mimetypes
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles


# Windows registry can mismap these; force standard types for console assets.
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/css", ".css")

from agent_flow.api.dependencies import AppServices, ReadinessCheck
from agent_flow.api.config import router as config_router
from agent_flow.api.health import router as health_router
from agent_flow.api.sessions import router as sessions_router
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


class _NoCacheStatic(StaticFiles):
    """Static assets that must always revalidate.

    Without an explicit Cache-Control, browsers apply heuristic freshness and
    serve stale JS/CSS from disk without revalidating — during iterative demos
    that means an old console UI with dead handlers. `no-cache` forces a
    revalidation each load; the existing etag keeps it a cheap 304.
    """

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


def mount_console(app: FastAPI) -> None:
    """Serve the packaged trace console at /console from wheel resources."""
    app.mount(
        "/console",
        _NoCacheStatic(packages=[("agent_flow", "console")], html=True),
        name="console",
    )


def create_app(
    *, pipeline=None, traces=None, conversations=None, authenticate=None,
    artifact_root: Path = Path("config"),
    dependency_checks: dict[str, object] | None = None,
    submissions=None, inbound=None,
    legacy_turn_timeout_seconds: float = 60.0,
    runtime_config=None,
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
        runtime_config=runtime_config,
    )
    app.include_router(turns_router)
    app.include_router(submissions_router)
    app.include_router(sessions_router)
    app.include_router(traces_router)
    app.include_router(config_router)
    app.include_router(health_router)
    mount_console(app)
    return app


app = create_app()
