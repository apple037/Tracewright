from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from fastapi import Header, HTTPException, Request
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


def services(request: Request) -> AppServices:
    return request.app.state.services


async def principal(
    request: Request, authorization: str | None = Header(default=None)
) -> AuthenticatedPrincipal:
    values = authorization.split(" ", 1) if authorization else []
    if len(values) != 2 or values[0].lower() != "bearer" or not values[1]:
        raise HTTPException(status_code=401, detail="authentication required")
    app_services = services(request)
    if app_services.authenticate is None:
        raise HTTPException(status_code=401, detail="authentication unavailable")
    authenticated = await app_services.authenticate(values[1])
    if authenticated is None:
        raise HTTPException(status_code=401, detail="authentication failed")
    return authenticated


def require_scope(authenticated: AuthenticatedPrincipal, *allowed: str) -> None:
    if authenticated.scopes.isdisjoint(allowed):
        raise HTTPException(status_code=403, detail="insufficient scope")
