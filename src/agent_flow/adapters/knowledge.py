"""Every knowledge source the pipeline can retrieve from, in one place.

`config/knowledge.yaml` lists the sources; this module turns that list into the
single RagClient the pipeline already expects, so adding a source is a config
edit rather than a code change.
"""

from __future__ import annotations

import asyncio
import json
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


def _read_config(path: Path) -> _KnowledgeConfig:
    return _KnowledgeConfig.model_validate(
        yaml.safe_load(path.read_text(encoding="utf-8")) or {}
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

    def catalog(self) -> tuple[str, ...]:
        # Also refreshed: a document the classifier never sees in the catalog is
        # a document it may not name, so it could never be retrieved.
        self._refresh()
        lines: list[str] = []
        for client in self._clients.values():
            lines.extend(getattr(client, "catalog", tuple)() or ())
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
