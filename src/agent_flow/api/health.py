from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from agent_flow.api.dependencies import services


router = APIRouter(prefix="/health")


@router.get("/live")
async def live():
    return {"status": "ok"}


@router.get("/ready")
async def ready(request: Request):
    app_services = services(request)
    checks = {
        "runtime_artifacts": app_services.artifact_status,
        "pipeline": "ok" if app_services.pipeline is not None else "missing",
        "trace_repository": "ok" if app_services.traces is not None else "missing",
        **app_services.dependency_checks,
    }
    healthy = all(value == "ok" for value in checks.values())
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={"status": "ready" if healthy else "not_ready", "checks": checks},
    )
