from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from agent_flow.api.dependencies import ReadinessCheck, services


router = APIRouter(prefix="/health")


@router.get("/live")
async def live():
    return {"status": "ok"}


@router.get("/ready")
async def ready(request: Request):
    app_services = services(request)
    checks: dict[str, ReadinessCheck] = {
        "runtime_artifacts": ReadinessCheck(status=_status(app_services.artifact_status)),
        "pipeline": ReadinessCheck(
            status="ok" if app_services.pipeline is not None else "missing"
        ),
        "trace_repository": ReadinessCheck(
            status="ok" if app_services.traces is not None else "missing"
        ),
        **app_services.dependency_checks,
    }
    healthy = all(check.status == "ok" for check in checks.values())
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={
            "status": "ready" if healthy else "not_ready",
            "checks": {
                name: check.model_dump(exclude_none=True)
                for name, check in checks.items()
            },
        },
    )


def _status(value: str) -> str:
    return value if value in {"ok", "missing", "invalid", "unavailable"} else "unavailable"
