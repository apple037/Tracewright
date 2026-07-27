"""Every knowledge source the pipeline can retrieve from, in one place.

`config/knowledge.yaml` lists the sources; this module turns that list into the
single RagClient the pipeline already expects, so adding a source is a config
edit rather than a code change.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from inspect import isawaitable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable
from urllib.parse import quote

import httpx
import yaml
from pydantic import Field

from agent_flow.adapters.evidence import MockRagClient, RagClient, rag_evidence
from agent_flow.auth import AuthorizedCustomerContext
from agent_flow.config import expand_env
from agent_flow.contracts import RagSearchRequest, RagSearchResult, StrictModel

if TYPE_CHECKING:
    from agent_flow.observability import OperationTelemetry


class _Source(StrictModel):
    type: str = Field(min_length=1)
    enabled: bool = True
    # type: fixture
    path: Path | None = None
    # type: http — an external knowledge base.
    catalog_url: str | None = None
    document_url: str | None = None
    auth_header: str = "Authorization"
    auth_header_env: str | None = None
    auth_prefix: str = "Bearer "
    timeout_seconds: float = Field(default=5.0, gt=0, le=60)
    # How long a fetched catalog is reused. Every turn asks for it, and a
    # knowledge base does not gain documents by the second.
    cache_seconds: float = Field(default=60.0, ge=0)
    # Response field -> key in the service's JSON. Defaults suit a service that
    # already speaks source_id/content/version.
    map: dict[str, str] = Field(default_factory=dict)


class _KnowledgeConfig(StrictModel):
    sources: dict[str, _Source] = Field(default_factory=dict)


def _build_fixture(
    name: str, source: _Source, telemetry: "OperationTelemetry | None"
) -> RagClient:
    if source.path is None:
        raise ValueError(f"knowledge source {name!r} of type fixture needs a path")
    return MockRagClient.from_fixture(source.path, telemetry=telemetry)


class HttpKnowledgeClient:
    """A corpus that lives in an external knowledge base.

    Two calls make a knowledge base usable here. `catalog_url` lists what it
    holds, as `source_id` plus a one-line summary; that list is what the
    classifier is shown, and it may only name a source it saw there. Naming one
    then fetches it from `document_url`. So the service decides what exists, and
    the grounding rule the pipeline depends on is unchanged: the assistant
    cannot cite a document the knowledge base never advertised.
    """

    def __init__(
        self,
        name: str,
        source: _Source,
        telemetry: "OperationTelemetry | None" = None,
    ) -> None:
        if not source.catalog_url or not source.document_url:
            raise ValueError(
                f"knowledge source {name!r} of type http needs catalog_url and "
                "document_url"
            )
        self._name = name
        self._source = source
        self.telemetry = telemetry
        self._catalog: tuple[str, ...] = ()
        self._ids: frozenset[str] = frozenset()
        self._fetched_at = 0.0

    def _headers(self) -> dict[str, str]:
        env = self._source.auth_header_env
        secret = os.environ.get(env, "") if env else ""
        if not secret:
            return {}
        return {self._source.auth_header: f"{self._source.auth_prefix}{secret}"}

    def _field(self, row: dict[str, Any], name: str, default: Any = None) -> Any:
        return row.get(self._source.map.get(name, name), default)

    async def _get(self, url: str, params: dict[str, str] | None = None) -> Any:
        async with httpx.AsyncClient(
            timeout=self._source.timeout_seconds
        ) as client:
            response = await client.get(
                url, headers=self._headers(), params=params or None
            )
            response.raise_for_status()
            return response.json()

    async def catalog(self, scope: dict[str, str] | None = None) -> tuple[str, ...]:
        fresh = time.monotonic() - self._fetched_at < self._source.cache_seconds
        if self._catalog and fresh:
            return self._catalog
        try:
            payload = await self._get(self._source.catalog_url or "", scope)
        except Exception:
            # A knowledge base that is down must not empty the catalog: that
            # would silently turn every grounded answer into "I don't know".
            # Keep serving the last list we were given.
            return self._catalog
        rows = payload.get("documents", payload) if isinstance(payload, dict) else payload
        lines: list[str] = []
        ids: list[str] = []
        for row in rows if isinstance(rows, list) else []:
            source_id = self._field(row, "source_id")
            if not source_id:
                continue
            summary = " ".join(str(self._field(row, "summary", "")).split())[:120]
            ids.append(str(source_id))
            lines.append(f"{source_id}: {summary}")
        self._catalog = tuple(lines[:50])
        self._ids = frozenset(ids)
        self._fetched_at = time.monotonic()
        return self._catalog

    @property
    def source_ids(self) -> frozenset[str]:
        # Whatever the last catalog advertised. Used only to route a query to
        # the source that owns it.
        return self._ids

    async def search(
        self, context: AuthorizedCustomerContext, request: RagSearchRequest
    ) -> RagSearchResult:
        started = time.monotonic()
        url = (self._source.document_url or "").replace(
            "{source_id}", quote(request.query, safe="")
        )
        # A knowledge base decides for itself what this caller may see, so who
        # is asking travels with the request. Expiry and per-customer documents
        # are its business, not ours — the same as any other backing service.
        scope = {"tenant_id": context.tenant_id, "customer_id": context.customer_id}
        try:
            payload = await self._get(url, scope)
        except Exception:
            if self.telemetry is not None:
                await self.telemetry.record_rag(
                    query=request.query, result_count=0, source_ids=[],
                    duration_ms=int((time.monotonic() - started) * 1000),
                    status="failed",
                )
            # Retrieving nothing is the safe failure: the generator then says it
            # does not have the information rather than inventing it.
            return RagSearchResult(items=())

        rows = payload if isinstance(payload, list) else [payload]
        items = tuple(
            rag_evidence(
                source_id=str(self._field(row, "source_id", request.query)),
                content=str(self._field(row, "content", "")),
                version=str(self._field(row, "version", "v1")),
                tenant_id=context.tenant_id,
                customer_id=self._field(row, "customer_id"),
            )
            for row in rows[: request.limit]
            if self._field(row, "content")
        )
        if self.telemetry is not None:
            await self.telemetry.record_rag(
                query=request.query, result_count=len(items),
                source_ids=[item.source_id for item in items],
                duration_ms=int((time.monotonic() - started) * 1000),
                status="completed",
            )
        return RagSearchResult(items=items)


def _build_http(
    name: str, source: _Source, telemetry: "OperationTelemetry | None"
) -> RagClient:
    return HttpKnowledgeClient(name, source, telemetry=telemetry)


# Register a new kind of source here: name -> builder. Nothing else changes.
_BUILDERS: dict[str, Callable[[str, _Source, Any], RagClient]] = {
    "fixture": _build_fixture,
    "http": _build_http,
}


def _read_config(path: Path) -> _KnowledgeConfig:
    return _KnowledgeConfig.model_validate(
        yaml.safe_load(expand_env(path.read_text(encoding="utf-8"))) or {}
    )


def _build_clients(
    config: _KnowledgeConfig, telemetry: "OperationTelemetry | None"
) -> dict[str, RagClient]:
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
    return clients


class KnowledgeSources:
    """Searches several sources as if they were one corpus."""

    def __init__(
        self,
        clients: dict[str, RagClient],
        *,
        config_path: Path | None = None,
        config: _KnowledgeConfig | None = None,
        telemetry: "OperationTelemetry | None" = None,
    ):
        self._clients = clients
        self._path = config_path
        self._config = config or _KnowledgeConfig()
        self._telemetry = telemetry
        self._stamp = self._current_stamp()

    @classmethod
    def from_config(
        cls, path: str | Path, *, telemetry: "OperationTelemetry | None" = None
    ) -> "KnowledgeSources":
        config_path = Path(path)
        config = _read_config(config_path)
        return cls(
            _build_clients(config, telemetry),
            config_path=config_path,
            config=config,
            telemetry=telemetry,
        )

    def _files(self) -> tuple[Path, ...]:
        if self._path is None:
            return ()
        paths = [self._path]
        paths.extend(s.path for s in self._config.sources.values() if s.path)
        return tuple(paths)

    def _current_stamp(self) -> tuple[tuple[str, int, int] | None, ...]:
        stamps: list[tuple[str, int, int] | None] = []
        for path in self._files():
            try:
                stat = path.stat()
            except OSError:
                stamps.append(None)
            else:
                stamps.append((str(path), stat.st_mtime_ns, stat.st_size))
        return tuple(stamps)

    def _refresh(self) -> None:
        """Pick up edits made by another process.

        The admin API runs in `app`; the pipeline searches from `worker`. Without
        this a document added through the API would not be retrievable until the
        worker restarted — the same trap console prompt edits fell into.
        """
        if self._path is None:
            return
        stamp = self._current_stamp()
        if stamp == self._stamp:
            return
        try:
            config = _read_config(self._path)
            clients = _build_clients(config, self._telemetry)
        except Exception:
            # A half-written or malformed file must not take retrieval down;
            # keep serving the corpus we already have and retry next search.
            self._stamp = stamp
            return
        self._config = config
        self._clients = clients
        self._stamp = self._current_stamp()

    async def search(
        self, context: AuthorizedCustomerContext, request: RagSearchRequest
    ) -> RagSearchResult:
        self._refresh()
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

    async def catalog(self) -> tuple[str, ...]:
        # Also refreshed: a document the classifier never sees in the catalog is
        # a document it may not name, so it could never be retrieved.
        self._refresh()
        lines: list[str] = []
        for client in self._clients.values():
            # A fixture answers from memory; a knowledge base has to be asked.
            entries = getattr(client, "catalog", tuple)() or ()
            if isawaitable(entries):
                entries = await entries or ()
            lines.extend(entries)
        return tuple(lines[:50])

    # --- admin surface -------------------------------------------------------
    # Editing what the assistant may state as fact. Every retrieved document is
    # treated as true and cited, so these are admin-only by design.

    def _fixture_path(self, source: str) -> Path:
        declared = self._config.sources.get(source)
        if declared is None:
            raise KeyError(source)
        if declared.type != "fixture" or declared.path is None:
            # Only a source backed by a file this process owns can be written.
            raise ValueError(f"knowledge source {source!r} is not editable")
        return declared.path

    def _load(self, path: Path) -> list[dict[str, Any]]:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise ValueError("knowledge fixture must contain a JSON array")
        return payload

    def _save(self, path: Path, rows: list[dict[str, Any]]) -> None:
        path.write_text(
            json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        self._refresh()

    def sources(self, tenant_id: str) -> list[dict[str, Any]]:
        """Every declared source and the documents this tenant owns."""
        listing = []
        for name, source in self._config.sources.items():
            editable = source.type == "fixture" and source.path is not None
            documents: list[dict[str, Any]] = []
            if editable:
                try:
                    documents = [
                        {
                            "source_id": row.get("source_id"),
                            "version": row.get("version", "v1"),
                            "content": row.get("content", ""),
                        }
                        for row in self._load(source.path)
                        if row.get("tenant_id") == tenant_id
                    ]
                except (OSError, ValueError, json.JSONDecodeError):
                    documents = []
            listing.append(
                {
                    "source": name,
                    "type": source.type,
                    "enabled": source.enabled,
                    "editable": editable,
                    "path": str(source.path) if source.path else None,
                    "documents": documents,
                }
            )
        return listing

    def put_document(
        self, source: str, source_id: str, content: str, tenant_id: str, version: str
    ) -> dict[str, Any]:
        path = self._fixture_path(source)
        rows = self._load(path)
        row = {
            "tenant_id": tenant_id,
            "source_id": source_id,
            "version": version,
            "content": content,
            "score": 1.0,
        }
        replaced = False
        for index, existing in enumerate(rows):
            if (
                existing.get("source_id") == source_id
                and existing.get("tenant_id") == tenant_id
            ):
                rows[index] = row
                replaced = True
                break
        if not replaced:
            rows.append(row)
        self._save(path, rows)
        return {"source": source, **row, "replaced": replaced}

    def delete_document(
        self, source: str, source_id: str, tenant_id: str
    ) -> dict[str, Any]:
        path = self._fixture_path(source)
        rows = self._load(path)
        kept = [
            row
            for row in rows
            if not (
                row.get("source_id") == source_id
                and row.get("tenant_id") == tenant_id
            )
        ]
        if len(kept) == len(rows):
            raise KeyError(source_id)
        self._save(path, kept)
        return {"source": source, "source_id": source_id, "deleted": True}
