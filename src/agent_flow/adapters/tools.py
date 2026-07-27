"""Every tool the pipeline can call, in one place.

`config/tools.yaml` lists them; this module turns that list into the single
ToolClient the pipeline already expects, so pointing a lookup at a real ERP or
CRM is a config edit rather than a code change.

A tool differs from a knowledge source in what it answers. Knowledge is a corpus
everyone shares — a refund policy, a group-buy list. A tool answers a question
about one customer, takes arguments, and is expected to be current: "where is
order-3". That is why the result carries the customer it was fetched for and how
fresh it is, and why the evidence validator can reject a stale answer.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable
from urllib.parse import quote

import httpx
import yaml
from pydantic import Field

from agent_flow.adapters.evidence import MockToolClient, ToolClient, tool_evidence
from agent_flow.auth import AuthorizedCustomerContext
from agent_flow.config import expand_env
from agent_flow.contracts import StrictModel, ToolCallRequest, ToolCallResult

if TYPE_CHECKING:
    from agent_flow.observability import OperationTelemetry


class _Tool(StrictModel):
    type: str = Field(min_length=1)
    enabled: bool = True
    # type: fixture
    path: Path | None = None
    # type: http
    url: str | None = None
    method: str = "GET"
    auth_header: str = "Authorization"
    auth_header_env: str | None = None
    auth_prefix: str = "Bearer "
    timeout_seconds: float = Field(default=5.0, gt=0, le=60)
    version: str = "v1"
    # Response field -> dotted path into the JSON body. Empty means "keep the
    # whole body", which is rarely what you want: the reply quotes these values.
    map: dict[str, str] = Field(default_factory=dict)


class _ToolConfig(StrictModel):
    tools: dict[str, _Tool] = Field(default_factory=dict)


def _dotted(payload: Any, path: str) -> Any:
    for key in path.split("."):
        if isinstance(payload, list):
            try:
                payload = payload[int(key)]
                continue
            except (ValueError, IndexError):
                return None
        if not isinstance(payload, dict):
            return None
        payload = payload.get(key)
    return payload


class HttpToolClient:
    """One tool backed by an HTTP service — an ERP, a CRM, an internal API."""

    def __init__(
        self,
        name: str,
        tool: _Tool,
        telemetry: "OperationTelemetry | None" = None,
    ) -> None:
        if not tool.url:
            raise ValueError(f"tool {name!r} of type http needs a url")
        self._name = name
        self._tool = tool
        self.telemetry = telemetry

    @property
    def tools(self) -> frozenset[str]:
        return frozenset({self._name})

    def _url(self, arguments: dict[str, Any]) -> str:
        # Arguments reach here from the model, so every one of them is escaped
        # before it becomes part of a path: an order id is a value, never a
        # path segment of its own.
        url = self._tool.url or ""
        for key, value in arguments.items():
            url = url.replace("{" + key + "}", quote(str(value), safe=""))
        return url

    def _headers(self) -> dict[str, str]:
        env = self._tool.auth_header_env
        secret = os.environ.get(env, "") if env else ""
        if not secret:
            return {}
        return {self._tool.auth_header: f"{self._tool.auth_prefix}{secret}"}

    def _result(self, body: Any) -> dict[str, Any]:
        if not self._tool.map:
            return body if isinstance(body, dict) else {"value": body}
        return {name: _dotted(body, path) for name, path in self._tool.map.items()}

    async def call(
        self, context: AuthorizedCustomerContext, request: ToolCallRequest
    ) -> ToolCallResult:
        started = time.monotonic()
        arguments = dict(request.arguments)
        try:
            async with httpx.AsyncClient(timeout=self._tool.timeout_seconds) as client:
                response = await client.request(
                    self._tool.method.upper(),
                    self._url(arguments),
                    headers=self._headers(),
                    params=arguments if self._tool.method.upper() == "GET" else None,
                    json=arguments if self._tool.method.upper() != "GET" else None,
                )
                response.raise_for_status()
                body = response.json()
        except Exception as error:
            if self.telemetry is not None:
                await self.telemetry.record_tool(
                    tool=request.tool, arguments=request.arguments,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    status="failed",
                )
            # Never surface the upstream body or URL: it can carry a credential
            # in a query string, and the pipeline turns this into a safe code.
            raise LookupError(f"tool {request.tool!r} did not answer") from error

        evidence = tool_evidence(
            tool=request.tool,
            arguments=arguments,
            result=self._result(body),
            version=self._tool.version,
            tenant_id=context.tenant_id,
            customer_id=context.customer_id,
        )
        if self.telemetry is not None:
            await self.telemetry.record_tool(
                tool=request.tool, arguments=request.arguments,
                duration_ms=int((time.monotonic() - started) * 1000),
                status="completed", result_source_id=evidence.source_id,
                freshness_seconds=evidence.metadata.get("freshness_seconds"),
            )
        return ToolCallResult(tool=request.tool, evidence=evidence)


def _build_fixture(
    name: str, tool: _Tool, telemetry: "OperationTelemetry | None"
) -> ToolClient:
    if tool.path is None:
        raise ValueError(f"tool {name!r} of type fixture needs a path")
    return MockToolClient.from_fixture(tool.path, telemetry=telemetry)


def _build_http(
    name: str, tool: _Tool, telemetry: "OperationTelemetry | None"
) -> ToolClient:
    return HttpToolClient(name, tool, telemetry=telemetry)


# Register a new kind of tool here: name -> builder. Nothing else changes.
_BUILDERS: dict[str, Callable[[str, _Tool, Any], ToolClient]] = {
    "fixture": _build_fixture,
    "http": _build_http,
}


class ToolSources:
    """Routes a tool call to whichever source declares that tool."""

    def __init__(self, clients: dict[str, ToolClient]):
        self._clients = clients

    @classmethod
    def from_config(
        cls, path: str | Path, *, telemetry: "OperationTelemetry | None" = None
    ) -> "ToolSources":
        config = _ToolConfig.model_validate(
            yaml.safe_load(expand_env(Path(path).read_text(encoding="utf-8"))) or {}
        )
        clients: dict[str, ToolClient] = {}
        for name, tool in config.tools.items():
            if not tool.enabled:
                continue
            builder = _BUILDERS.get(tool.type)
            if builder is None:
                raise ValueError(
                    f"tool {name!r} has unknown type {tool.type!r}; "
                    f"known types: {', '.join(sorted(_BUILDERS))}"
                )
            clients[name] = builder(name, tool, telemetry)
        return cls(clients)

    async def call(
        self, context: AuthorizedCustomerContext, request: ToolCallRequest
    ) -> ToolCallResult:
        for client in self._clients.values():
            if request.tool in getattr(client, "tools", frozenset()):
                return await client.call(context, request)
        # A fixture source answers for whatever its records name, so fall back
        # to whoever accepts the call rather than requiring it to be declared.
        for client in self._clients.values():
            try:
                return await client.call(context, request)
            except LookupError:
                continue
        raise LookupError("tool not found in authorized scope")
