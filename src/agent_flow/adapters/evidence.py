import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, Self

from pydantic import Field

from agent_flow.auth import AuthorizedCustomerContext
from agent_flow.contracts import (
    EvidenceItem,
    RagSearchRequest,
    RagSearchResult,
    StrictModel,
    ToolCallRequest,
    ToolCallResult,
)


FIXTURE_EFFECTIVE_AT = datetime(2025, 1, 1, tzinfo=timezone.utc)
FIXTURE_RETRIEVED_AT = datetime(2026, 1, 1, tzinfo=timezone.utc)
FIXTURE_VALID_UNTIL = datetime(2099, 12, 31, tzinfo=timezone.utc)


class RagClient(Protocol):
    async def search(
        self, context: AuthorizedCustomerContext, request: RagSearchRequest
    ) -> RagSearchResult: ...


class ToolClient(Protocol):
    async def call(
        self, context: AuthorizedCustomerContext, request: ToolCallRequest
    ) -> ToolCallResult: ...


class _RagFixture(StrictModel):
    tenant_id: str = Field(min_length=1)
    customer_id: str | None = None
    source_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    content: str = Field(min_length=1)
    retrieved_at: datetime = FIXTURE_RETRIEVED_AT
    effective_at: datetime | None = FIXTURE_EFFECTIVE_AT
    valid_until: datetime | None = None
    score: float | None = 1.0
    metadata: dict[str, Any] = Field(default_factory=dict)


class _ToolFixture(StrictModel):
    tenant_id: str = Field(min_length=1)
    customer_id: str = Field(min_length=1)
    tool: str = Field(min_length=1)
    arguments: dict[str, Any]
    result: dict[str, Any]
    version: str = Field(default="v1", min_length=1)
    retrieved_at: datetime = FIXTURE_RETRIEVED_AT
    effective_at: datetime | None = FIXTURE_EFFECTIVE_AT
    valid_until: datetime | None = FIXTURE_VALID_UNTIL
    score: float | None = 1.0


def _require_context(context: AuthorizedCustomerContext) -> None:
    if not isinstance(context, AuthorizedCustomerContext):
        raise TypeError("context must be an AuthorizedCustomerContext")
    if not context.tenant_id or not context.customer_id:
        raise ValueError("authorized context must bind tenant and customer")


def _checksum(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _fixture_payload(path: str | Path) -> list[dict[str, Any]]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("evidence fixture must contain a JSON array")
    return payload


class MockRagClient:
    def __init__(
        self,
        records: tuple[_RagFixture, ...],
        *,
        as_of: datetime = FIXTURE_RETRIEVED_AT,
    ) -> None:
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("as_of must be timezone-aware")
        self._records = records
        self._as_of = as_of

    @classmethod
    def from_fixture(
        cls,
        path: str | Path,
        *,
        as_of: datetime = FIXTURE_RETRIEVED_AT,
    ) -> Self:
        return cls(
            tuple(_RagFixture.model_validate(row) for row in _fixture_payload(path)),
            as_of=as_of,
        )

    async def search(
        self, context: AuthorizedCustomerContext, request: RagSearchRequest
    ) -> RagSearchResult:
        _require_context(context)
        eligible = (
            record
            for record in self._records
            if record.tenant_id == context.tenant_id
            and record.customer_id in (None, context.customer_id)
            and (record.effective_at is None or record.effective_at <= self._as_of)
            and (record.valid_until is None or record.valid_until > self._as_of)
        )
        ordered = sorted(
            eligible,
            key=lambda record: (
                -(record.score if record.score is not None else float("-inf")),
                record.source_id,
                record.version,
            ),
        )
        items = tuple(self._evidence(record) for record in ordered[: request.limit])
        return RagSearchResult(items=items)

    def _evidence(self, record: _RagFixture) -> EvidenceItem:
        checksum = _checksum(record.content)
        metadata = {
            **record.metadata,
            "tenant_id": record.tenant_id,
            "customer_id": record.customer_id,
        }
        return EvidenceItem(
            evidence_id=f"rag:{record.source_id}:{record.version}:{checksum[:16]}",
            source_id=record.source_id,
            version=record.version,
            content=record.content,
            content_checksum=checksum,
            retrieved_at=self._as_of,
            effective_at=record.effective_at,
            valid_until=record.valid_until,
            score=record.score,
            metadata=metadata,
        )


class MockToolClient:
    def __init__(self, records: tuple[_ToolFixture, ...]) -> None:
        self._records = records

    @classmethod
    def from_fixture(cls, path: str | Path) -> Self:
        return cls(
            tuple(_ToolFixture.model_validate(row) for row in _fixture_payload(path))
        )

    async def call(
        self, context: AuthorizedCustomerContext, request: ToolCallRequest
    ) -> ToolCallResult:
        _require_context(context)
        requested_arguments = _canonical_json(request.arguments)
        record = next(
            (
                candidate
                for candidate in self._records
                if candidate.tenant_id == context.tenant_id
                and candidate.customer_id == context.customer_id
                and candidate.tool == request.tool
                and _canonical_json(candidate.arguments) == requested_arguments
            ),
            None,
        )
        if record is None:
            raise LookupError("tool fixture not found in authorized scope")

        content = _canonical_json(record.result)
        checksum = _checksum(content)
        freshness_seconds = None
        if record.valid_until is not None:
            freshness_seconds = max(
                0, int((record.valid_until - record.retrieved_at).total_seconds())
            )
        evidence = EvidenceItem(
            evidence_id=f"tool:{record.tool}:{checksum[:16]}",
            source_id=f"tool:{record.tool}",
            version=record.version,
            content=content,
            content_checksum=checksum,
            retrieved_at=record.retrieved_at,
            effective_at=record.effective_at,
            valid_until=record.valid_until,
            score=record.score,
            metadata={
                "tenant_id": record.tenant_id,
                "customer_id": record.customer_id,
                "tool": record.tool,
                "arguments": record.arguments,
                "freshness_seconds": freshness_seconds,
            },
        )
        return ToolCallResult(tool=request.tool, evidence=evidence)
