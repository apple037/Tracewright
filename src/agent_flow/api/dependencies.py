from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from agent_flow.auth import AuthenticatedPrincipal


Authenticator = Callable[[str], Awaitable[AuthenticatedPrincipal | None]]


class ReadinessCheck(BaseModel):
    status: Literal["ok", "missing", "invalid", "unavailable"]
    role: str | None = None
    stage: str | None = None
    error_code: str | None = None


@dataclass(frozen=True)
class AppServices:
    pipeline: object | None
    traces: object | None
    conversations: object | None
    authenticate: Authenticator | None
    artifact_root: Path
    artifact_status: str
    dependency_checks: dict[str, ReadinessCheck]
    submissions: object | None = None
    inbound: object | None = None
    legacy_turn_timeout_seconds: float = 60.0
    runtime_config: object | None = None
    # The editable knowledge corpus, when the runtime is backed by one.
    knowledge: object | None = None
    # Re-runs the model capability probe. The startup probe is a snapshot: boot
    # while the model server is still loading and a cached failure kept the app
    # not_ready forever, needing a restart it did not deserve.
    recheck_models: object | None = None


def services(request: Request) -> AppServices:
    return request.app.state.services


# Declared as a security scheme rather than a plain header so /docs renders one
# Authorize button for the whole API, instead of an authorization box to paste
# a token into on each of sixteen endpoints. auto_error=False keeps the 401
# body ours.
bearer_scheme = HTTPBearer(
    auto_error=False,
    description="A demo token from .env — DEMO_ADMIN_TOKEN or DEMO_CUSTOMER_TOKEN.",
)


async def principal(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> AuthenticatedPrincipal:
    if (
        credentials is None
        or credentials.scheme.lower() != "bearer"
        or not credentials.credentials
    ):
        raise HTTPException(status_code=401, detail="authentication required")
    app_services = services(request)
    if app_services.authenticate is None:
        raise HTTPException(status_code=401, detail="authentication unavailable")
    authenticated = await app_services.authenticate(credentials.credentials)
    if authenticated is None:
        raise HTTPException(status_code=401, detail="authentication failed")
    return authenticated


def require_scope(authenticated: AuthenticatedPrincipal, *allowed: str) -> None:
    if authenticated.scopes.isdisjoint(allowed):
        raise HTTPException(status_code=403, detail="insufficient scope")
