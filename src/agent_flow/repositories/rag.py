import hashlib
import math
from collections.abc import Sequence
from typing import Any

from agent_flow.auth import AuthorizedCustomerContext
from agent_flow.contracts import EvidenceItem, RagSearchRequest, RagSearchResult
from agent_flow.repositories.postgres import PostgresPool


EMBEDDING_DIMENSIONS = 1024


def _vector_literal(embedding: Sequence[float]) -> str:
    if len(embedding) != EMBEDDING_DIMENSIONS:
        raise ValueError(f"embedding must contain exactly {EMBEDDING_DIMENSIONS} values")
    values = tuple(float(value) for value in embedding)
    if not all(math.isfinite(value) for value in values):
        raise ValueError("embedding values must be finite")
    if math.hypot(*values) == 0.0:
        raise ValueError("embedding must have a non-zero norm")
    return "[" + ",".join(format(value, ".17g") for value in values) + "]"


class RagRepository:
    def __init__(self, pool: PostgresPool) -> None:
        self._pool = pool

    async def search_cosine(
        self,
        context: AuthorizedCustomerContext,
        request: RagSearchRequest,
        embedding: Sequence[float],
    ) -> RagSearchResult:
        if not isinstance(context, AuthorizedCustomerContext):
            raise TypeError("context must be an AuthorizedCustomerContext")
        if not context.tenant_id or not context.customer_id:
            raise ValueError("authorized context must bind tenant and customer")
        vector = _vector_literal(embedding)

        async with self._pool.connection() as connection:
            cursor = await connection.execute(
                """
                WITH candidates AS (
                    SELECT
                        c.id AS evidence_id,
                        d.source_id,
                        d.version,
                        c.content,
                        statement_timestamp() AS retrieved_at,
                        d.effective_at,
                        d.valid_until,
                        c.embedding <=> %s::vector AS distance,
                        d.access_metadata,
                        c.metadata AS chunk_metadata
                    FROM rag.chunks AS c
                    JOIN rag.documents AS d ON d.id = c.document_id
                    WHERE d.tenant_id = %s
                      AND c.tenant_id = %s
                      AND (d.customer_id IS NULL OR d.customer_id = %s)
                      AND (c.customer_id IS NULL OR c.customer_id = %s)
                      AND d.ingestion_status = 'ready'
                      AND (d.effective_at IS NULL OR d.effective_at <= statement_timestamp())
                      AND (d.valid_until IS NULL OR d.valid_until > statement_timestamp())
                )
                SELECT
                    evidence_id, source_id, version, content, retrieved_at,
                    effective_at, valid_until, 1 - distance AS score,
                    access_metadata, chunk_metadata
                FROM candidates
                WHERE distance BETWEEN 0.0 AND 2.0
                ORDER BY distance, evidence_id
                LIMIT %s
                """,
                (
                    vector,
                    context.tenant_id,
                    context.tenant_id,
                    context.customer_id,
                    context.customer_id,
                    request.limit,
                ),
            )
            rows = await cursor.fetchall()

        return RagSearchResult(items=tuple(self._evidence(row) for row in rows))

    @staticmethod
    def _evidence(row: dict[str, Any]) -> EvidenceItem:
        content_checksum = hashlib.sha256(row["content"].encode("utf-8")).hexdigest()
        score = float(row["score"])
        if not math.isfinite(score):
            raise ValueError("cosine search returned a non-finite score")
        return EvidenceItem(
            evidence_id=str(row["evidence_id"]),
            source_id=row["source_id"],
            version=row["version"],
            content=row["content"],
            content_checksum=content_checksum,
            retrieved_at=row["retrieved_at"],
            effective_at=row["effective_at"],
            valid_until=row["valid_until"],
            score=score,
            metadata={
                "access": row["access_metadata"],
                "chunk": row["chunk_metadata"],
                "distance_metric": "cosine",
                "search_mode": "exact",
            },
        )
