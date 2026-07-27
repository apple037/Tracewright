"""Every knowledge source the pipeline can retrieve from, in one place.

`config/knowledge.yaml` lists the sources; this module turns that list into the
single RagClient the pipeline already expects, so adding a source is a config
edit rather than a code change.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

import yaml
from pydantic import Field

from agent_flow.adapters.evidence import MockRagClient, RagClient
from agent_flow.auth import AuthorizedCustomerContext
from agent_flow.contracts import RagSearchRequest, RagSearchResult, StrictModel

if TYPE_CHECKING:
    from agent_flow.observability import OperationTelemetry


class _Source(StrictModel):
    type: str = Field(min_length=1)
    path: Path | None = None
    enabled: bool = True


class _KnowledgeConfig(StrictModel):
    sources: dict[str, _Source] = Field(default_factory=dict)


def _build_fixture(
    name: str, source: _Source, telemetry: "OperationTelemetry | None"
) -> RagClient:
    if source.path is None:
        raise ValueError(f"knowledge source {name!r} of type fixture needs a path")
    return MockRagClient.from_fixture(source.path, telemetry=telemetry)


# Register a new kind of source here: name -> builder. Nothing else changes.
_BUILDERS: dict[str, Callable[[str, _Source, Any], RagClient]] = {
    "fixture": _build_fixture,
}


class KnowledgeSources:
    """Searches several sources as if they were one corpus."""

    def __init__(self, clients: dict[str, RagClient]):
        self._clients = clients

    @classmethod
    def from_config(
        cls, path: str | Path, *, telemetry: "OperationTelemetry | None" = None
    ) -> "KnowledgeSources":
        config = _KnowledgeConfig.model_validate(
            yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        )
        clients: dict[str, RagClient] = {}
        for name, source in config.sources.items():
            if not source.enabled:
                continue
            builder = _BUILDERS.get(source.type)
            if builder is None:
                raise ValueError(
                    f"knowledge source {name!r} has unknown type {source.type!r}; "
                    f"known types: {', '.join(sorted(_BUILDERS))}"
                )
            clients[name] = builder(name, source, telemetry)
        return cls(clients)

    async def search(
        self, context: AuthorizedCustomerContext, request: RagSearchRequest
    ) -> RagSearchResult:
        # The pipeline queries by a source_id from catalog(), so when one source
        # owns that id the others are not searched at all.
        owners = [
            client
            for client in self._clients.values()
            if request.query in getattr(client, "source_ids", frozenset())
        ]
        targets = owners or list(self._clients.values())
        results = await asyncio.gather(
            *(client.search(context, request) for client in targets)
        )
        items = [item for result in results for item in result.items]
        items.sort(
            key=lambda item: (
                -(item.score if item.score is not None else float("-inf")),
                item.source_id,
                item.version,
            )
        )
        return RagSearchResult(items=tuple(items[: request.limit]))

    def catalog(self) -> tuple[str, ...]:
        lines: list[str] = []
        for client in self._clients.values():
            lines.extend(getattr(client, "catalog", tuple)() or ())
        return tuple(lines[:50])
