"""A stand-in knowledge base, so the demo exercises the external path.

The real design is that a corpus lives in a knowledge base the team already
runs, and Tracewright asks it two questions: what do you hold, and give me this
document. Nobody should have to stand up a knowledge base to try the demo, so
this serves the committed demo corpus over exactly that interface.

It is deliberately dumb and deliberately unauthenticated: it is a fake service
that happens to be hosted in the same process, not a feature. Point
`config/knowledge.yaml` at a real knowledge base and delete nothing — the
`type: http` source does not know the difference.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException


router = APIRouter(prefix="/mock-kb", tags=["Mock knowledge base"])

_CORPUS = (
    Path("config/demo/rag.json"),
    Path("config/demo/groupbuy.json"),
)


def _live(row: dict[str, Any]) -> bool:
    """Expiry belongs to whoever owns the corpus, which is now this service.

    A knowledge base that keeps serving last summer's promotion is the reason
    an assistant quotes a price that no longer exists.
    """
    if row.get("customer_id"):
        # Documents bound to one customer stay in a local source, where the
        # per-customer rule is enforced. This stand-in has no idea who is
        # asking, so it must not hold them at all.
        return False
    valid_until = row.get("valid_until")
    if not valid_until:
        return True
    try:
        return datetime.fromisoformat(valid_until) > datetime.now(timezone.utc)
    except ValueError:
        return True


def _documents() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in _CORPUS:
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, list):
            rows.extend(
                row for row in payload if isinstance(row, dict) and _live(row)
            )
    return rows


_SUMMARY_LENGTH = 120


@router.get("/documents")
async def list_documents():
    """What this knowledge base holds: an id and a one-line summary each.

    This is the list the classifier is shown, and it may only name a source it
    saw here — so a document missing from this response can never be cited.
    """
    return {
        "documents": [
            {
                "source_id": row.get("source_id"),
                "summary": " ".join(str(row.get("content", "")).split())[
                    :_SUMMARY_LENGTH
                ],
            }
            for row in _documents()
            if row.get("source_id")
        ]
    }


@router.get("/documents/{source_id:path}")
async def get_document(source_id: str):
    """One document, in full."""
    for row in _documents():
        if row.get("source_id") == source_id:
            return {
                "source_id": row["source_id"],
                "version": row.get("version", "v1"),
                "content": row.get("content", ""),
            }
    raise HTTPException(status_code=404, detail="document not found")
