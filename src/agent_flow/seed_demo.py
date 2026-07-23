from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from psycopg.types.json import Jsonb

from agent_flow.config import Settings
from agent_flow.repositories.postgres import PostgresPool


EMBEDDING_DIMENSIONS = 1024
DEMO_SOURCE_PREFIX = "agent-flow-demo:"
DEMO_FIXTURE_OWNER = "agent_flow.seed_demo"
DEMO_OWNERSHIP = {"demo": True, "fixture_owner": DEMO_FIXTURE_OWNER}


class DemoSeedCollisionError(ValueError):
    """Raised when a demo fixture key is already owned by non-demo data."""


@dataclass(frozen=True)
class DemoRagDocument:
    tenant_id: str
    customer_id: str | None
    source_id: str
    version: str
    content: str
    valid_until: datetime | None
    embedding: tuple[float, ...]
    fixture_type: str


@dataclass(frozen=True)
class DemoToolFixture:
    tenant_id: str
    customer_id: str
    tool: str
    arguments: dict[str, Any]
    result: dict[str, Any]


@dataclass(frozen=True)
class DemoFixtures:
    rag_documents: tuple[DemoRagDocument, ...]
    tool_fixtures: tuple[DemoToolFixture, ...]


@dataclass(frozen=True)
class DemoSeedResult:
    rag_documents_seeded: int
    tool_fixtures_loaded: int


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _deterministic_embedding(content: str) -> tuple[float, ...]:
    raw = hashlib.shake_256(content.encode("utf-8")).digest(EMBEDDING_DIMENSIONS)
    return tuple((value - 127.5) / 127.5 for value in raw)


def _required_text(item: dict[str, Any], key: str) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"demo fixture requires non-empty {key}")
    return value


def _read_list(path: Path) -> list[dict[str, Any]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError(f"{path.name} must contain a JSON object list")
    return value


def load_demo_fixtures(fixture_root: Path, *, tenant_id: str) -> DemoFixtures:
    if not tenant_id:
        raise ValueError("tenant_id must be non-empty")
    rag_documents = []
    for item in _read_list(fixture_root / "rag.json"):
        if item.get("tenant_id") != tenant_id:
            continue
        content = _required_text(item, "content")
        valid_until = item.get("valid_until")
        rag_documents.append(
            DemoRagDocument(
                tenant_id=tenant_id,
                customer_id=item.get("customer_id"),
                source_id=DEMO_SOURCE_PREFIX + _required_text(item, "source_id"),
                version=_required_text(item, "version"),
                content=content,
                valid_until=(
                    datetime.fromisoformat(valid_until.replace("Z", "+00:00"))
                    if isinstance(valid_until, str)
                    else None
                ),
                embedding=_deterministic_embedding(content),
                fixture_type="mock_rag",
            )
        )
    tool_fixtures = []
    for item in _read_list(fixture_root / "tools.json"):
        if item.get("tenant_id") != tenant_id:
            continue
        arguments = item.get("arguments")
        result = item.get("result")
        if not isinstance(arguments, dict) or not isinstance(result, dict):
            raise ValueError("tool fixtures require object arguments and result")
        tool_fixtures.append(
            DemoToolFixture(
                tenant_id=tenant_id,
                customer_id=_required_text(item, "customer_id"),
                tool=_required_text(item, "tool"),
                arguments=arguments,
                result=result,
            )
        )
    if not rag_documents or not tool_fixtures:
        raise ValueError(f"no complete demo fixtures found for tenant {tenant_id}")
    return DemoFixtures(tuple(rag_documents), tuple(tool_fixtures))


def _stable_id(kind: str, *parts: str) -> UUID:
    return uuid5(NAMESPACE_URL, ":".join(("agent-flow-demo", kind, *parts)))


def _vector_literal(values: tuple[float, ...]) -> str:
    return "[" + ",".join(format(value, ".17g") for value in values) + "]"


class DemoSeeder:
    def __init__(
        self,
        pool: PostgresPool,
        *,
        fixture_root: Path = Path("tests/fixtures"),
        tenant_id: str = "t1",
    ) -> None:
        self._pool = pool
        self._fixture_root = fixture_root
        self._tenant_id = tenant_id

    async def seed(self) -> DemoSeedResult:
        fixtures = load_demo_fixtures(
            self._fixture_root, tenant_id=self._tenant_id
        )
        async with self._pool.connection() as connection:
            async with connection.transaction():
                for document in fixtures.rag_documents:
                    await self._upsert_document(connection, document)
        return DemoSeedResult(
            rag_documents_seeded=len(fixtures.rag_documents),
            tool_fixtures_loaded=len(fixtures.tool_fixtures),
        )

    @staticmethod
    async def _upsert_document(connection, document: DemoRagDocument) -> None:
        checksum = hashlib.sha256(document.content.encode("utf-8")).hexdigest()
        ownership = {
            **DEMO_OWNERSHIP,
            "fixture_type": document.fixture_type,
        }
        requested_id = _stable_id(
            "document",
            document.tenant_id,
            document.source_id,
            document.version,
        )
        cursor = await connection.execute(
            """
            INSERT INTO rag.documents (
                id, tenant_id, customer_id, source_id, version, checksum,
                access_metadata, ingestion_status, effective_at, valid_until
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'ready', now(), %s)
            ON CONFLICT (tenant_id, source_id, version) DO UPDATE
            SET customer_id = EXCLUDED.customer_id,
                checksum = EXCLUDED.checksum,
                access_metadata = EXCLUDED.access_metadata,
                ingestion_status = 'ready',
                effective_at = EXCLUDED.effective_at,
                valid_until = EXCLUDED.valid_until
            WHERE rag.documents.id = EXCLUDED.id
              AND rag.documents.access_metadata @> %s
            RETURNING id
            """,
            (
                requested_id,
                document.tenant_id,
                document.customer_id,
                document.source_id,
                document.version,
                checksum,
                Jsonb(ownership),
                document.valid_until,
                Jsonb(DEMO_OWNERSHIP),
            ),
        )
        stored = await cursor.fetchone()
        if stored is None:
            raise DemoSeedCollisionError(
                "demo seed document collision: source key is not owned by this seeder"
            )
        stored_id = stored["id"]
        chunk_id = _stable_id("chunk", str(stored_id), "0")
        cursor = await connection.execute(
            """
            INSERT INTO rag.chunks (
                id, document_id, tenant_id, customer_id, ordinal,
                content, metadata, embedding
            ) VALUES (%s, %s, %s, %s, 0, %s, %s, %s::vector)
            ON CONFLICT (document_id, ordinal) DO UPDATE
            SET tenant_id = EXCLUDED.tenant_id,
                customer_id = EXCLUDED.customer_id,
                content = EXCLUDED.content,
                metadata = EXCLUDED.metadata,
                embedding = EXCLUDED.embedding
            WHERE rag.chunks.id = EXCLUDED.id
              AND rag.chunks.metadata @> %s
            RETURNING id
            """,
            (
                chunk_id,
                stored_id,
                document.tenant_id,
                document.customer_id,
                document.content,
                Jsonb(ownership),
                _vector_literal(document.embedding),
                Jsonb(DEMO_OWNERSHIP),
            ),
        )
        if await cursor.fetchone() is None:
            raise DemoSeedCollisionError(
                "demo seed chunk collision: ordinal is not owned by this seeder"
            )


async def _run_seed() -> DemoSeedResult:
    settings = Settings()
    pool = PostgresPool(settings.database_url)
    await pool.open()
    try:
        return await DemoSeeder(
            pool,
            tenant_id=os.getenv("DEMO_TENANT_ID", "t1"),
        ).seed()
    finally:
        await pool.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed committed Agent Flow demo fixtures")
    parser.parse_args()
    result = asyncio.run(_run_seed())
    print(
        _canonical_json(
            {
                "event": "demo_seed_completed",
                "rag_documents_seeded": result.rag_documents_seeded,
                "tool_fixtures_loaded": result.tool_fixtures_loaded,
            }
        )
    )


if __name__ == "__main__":
    main()
