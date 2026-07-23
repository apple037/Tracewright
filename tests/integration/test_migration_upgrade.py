import asyncio
import os
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import dict_row


VERSIONS = Path("migrations/versions")


def test_bootstrap_revision_is_immutable_and_forward_revision_owns_new_storage():
    bootstrap = (VERSIONS / "0001_bootstrap_runtime.py").read_text(encoding="utf-8")
    forward = (VERSIONS / "0002_handoff_outbox.py").read_text(encoding="utf-8")
    assert '"turn_inputs"' not in bootstrap
    assert '"claim_token"' not in bootstrap
    assert '"lease_expires_at"' not in bootstrap
    assert 'down_revision = "0001_bootstrap_runtime"' in forward
    assert '"turn_inputs"' in forward
    assert '"claim_token"' in forward
    assert '"lease_expires_at"' in forward


def _database_url(base_url: str, database: str) -> str:
    parsed = urlsplit(base_url)
    return urlunsplit(parsed._replace(path=f"/{database}"))


@asynccontextmanager
async def _scratch_database(base_url: str, label: str):
    name = f"agent_flow_task10_test_{label}_{uuid4().hex}"
    connection = await psycopg.AsyncConnection.connect(base_url, autocommit=True)
    try:
        await connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        yield _database_url(base_url, name)
    finally:
        await connection.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid()",
            (name,),
        )
        await connection.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))
        await connection.close()


async def _upgrade(database_url: str, target: str) -> None:
    environment = os.environ.copy()
    environment["DATABASE_URL"] = database_url
    result = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "alembic", "upgrade", target],
        cwd=Path.cwd(),
        env=environment,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr


async def _schema_state(database_url: str) -> dict[str, object]:
    connection = await psycopg.AsyncConnection.connect(
        database_url, row_factory=dict_row
    )
    try:
        cursor = await connection.execute(
            """
            SELECT
              to_regclass('runtime.turn_inputs') IS NOT NULL AS has_turn_inputs,
              EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'notification' AND table_name = 'outbox'
                  AND column_name = 'claim_token'
              ) AS has_claim_token,
              (
                SELECT is_nullable = 'YES' FROM information_schema.columns
                WHERE table_schema = 'notification' AND table_name = 'outbox'
                  AND column_name = 'next_attempt_at'
              ) AS next_attempt_nullable,
              (SELECT version_num FROM alembic_version) AS revision
            """
        )
        return await cursor.fetchone()
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_existing_0001_database_upgrades_to_head(test_database_url):
    async with _scratch_database(test_database_url, "upgrade") as database_url:
        await _upgrade(database_url, "0001_bootstrap_runtime")
        at_bootstrap = await _schema_state(database_url)
        assert at_bootstrap == {
            "has_turn_inputs": False,
            "has_claim_token": False,
            "next_attempt_nullable": False,
            "revision": "0001_bootstrap_runtime",
        }
        await _upgrade(database_url, "head")
        assert await _schema_state(database_url) == {
            "has_turn_inputs": True,
            "has_claim_token": True,
            "next_attempt_nullable": True,
            "revision": "0002_handoff_outbox",
        }


@pytest.mark.asyncio
async def test_fresh_database_upgrades_directly_to_head(test_database_url):
    async with _scratch_database(test_database_url, "fresh") as database_url:
        await _upgrade(database_url, "head")
        assert await _schema_state(database_url) == {
            "has_turn_inputs": True,
            "has_claim_token": True,
            "next_attempt_nullable": True,
            "revision": "0002_handoff_outbox",
        }
