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


def test_task9_bootstrap_and_forward_compatibility_revision_are_explicit():
    bootstrap = (VERSIONS / "0001_bootstrap_runtime.py").read_text(encoding="utf-8")
    forward = (VERSIONS / "0002_handoff_outbox.py").read_text(encoding="utf-8")
    assert '"turn_inputs"' in bootstrap
    assert '"claim_token"' not in bootstrap
    assert '"lease_expires_at"' not in bootstrap
    assert 'down_revision = "0001_bootstrap_runtime"' in forward
    assert "same-revision historical drift" in forward
    assert "inspect(" in forward
    assert '"claim_token"' in forward
    assert '"lease_expires_at"' in forward


def test_turn_submission_revision_declares_channel_and_job_schema():
    revision = (VERSIONS / "0003_turn_submissions.py").read_text(encoding="utf-8")

    assert 'down_revision = "0002_handoff_outbox"' in revision
    for name in (
        "channel",
        "external_message_id",
        "trace_id",
        "result",
        "finished_at",
        "lease_expires_at",
        "claim_token",
        "ix_jobs_trace",
    ):
        assert f'"{name}"' in revision
    assert "'queued'" in revision
    assert "inspect(" in revision


def _database_url(base_url: str, database: str) -> str:
    parsed = urlsplit(base_url)
    return urlunsplit(parsed._replace(path=f"/{database}"))


@asynccontextmanager
async def _scratch_database(base_url: str, label: str):
    name = f"agent_flow_task10_test_{label}_{uuid4().hex}"
    connection = await psycopg.AsyncConnection.connect(base_url, autocommit=True)
    try:
        await connection.execute(
            sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name))
        )
        yield _database_url(base_url, name)
    finally:
        await connection.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid()",
            (name,),
        )
        await connection.execute(
            sql.SQL("DROP DATABASE {}").format(sql.Identifier(name))
        )
        await connection.close()


async def _migrate(database_url: str, action: str, target: str) -> None:
    environment = os.environ.copy()
    environment["DATABASE_URL"] = database_url
    result = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "alembic", action, target],
        cwd=Path.cwd(),
        env=environment,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr


async def _upgrade(database_url: str, target: str) -> None:
    await _migrate(database_url, "upgrade", target)


async def _downgrade(database_url: str, target: str) -> None:
    await _migrate(database_url, "downgrade", target)


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
              EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'notification' AND table_name = 'outbox'
                  AND column_name = 'settlement_backoff_seconds'
              ) AS has_settlement_backoff,
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


async def _submission_schema_state(database_url: str) -> dict[str, object]:
    connection = await psycopg.AsyncConnection.connect(
        database_url, row_factory=dict_row
    )
    try:
        cursor = await connection.execute(
            """
            SELECT
              (
                SELECT jsonb_object_agg(
                  column_name,
                  jsonb_build_object(
                    'data_type', data_type,
                    'udt_name', udt_name,
                    'nullable', is_nullable = 'YES'
                  )
                )
                FROM information_schema.columns
                WHERE table_schema = 'observability' AND table_name = 'traces'
                  AND column_name IN ('channel', 'external_message_id')
              ) AS trace_columns,
              (
                SELECT jsonb_object_agg(
                  column_name,
                  jsonb_build_object(
                    'data_type', data_type,
                    'udt_name', udt_name,
                    'nullable', is_nullable = 'YES'
                  )
                )
                FROM information_schema.columns
                WHERE table_schema = 'runtime' AND table_name = 'jobs'
                  AND column_name IN (
                    'trace_id', 'result', 'finished_at',
                    'lease_expires_at', 'claim_token'
                  )
              ) AS job_columns,
              EXISTS (
                SELECT 1
                FROM pg_constraint c
                JOIN pg_class source ON source.oid = c.conrelid
                JOIN pg_namespace source_ns ON source_ns.oid = source.relnamespace
                JOIN pg_class target ON target.oid = c.confrelid
                JOIN pg_namespace target_ns ON target_ns.oid = target.relnamespace
                WHERE c.contype = 'f'
                  AND source_ns.nspname = 'runtime' AND source.relname = 'jobs'
                  AND target_ns.nspname = 'observability'
                  AND target.relname = 'traces'
                  AND c.confdeltype = 'c'
                  AND pg_get_constraintdef(c.oid) LIKE 'FOREIGN KEY (trace_id)%'
              ) AS has_trace_foreign_key,
              EXISTS (
                SELECT 1 FROM pg_indexes
                WHERE schemaname = 'runtime' AND tablename = 'jobs'
                  AND indexname = 'ix_jobs_trace'
                  AND indexdef LIKE '%(trace_id)%'
              ) AS has_trace_index,
              (
                SELECT pg_get_constraintdef(c.oid)
                FROM pg_constraint c
                JOIN pg_class relation ON relation.oid = c.conrelid
                JOIN pg_namespace namespace ON namespace.oid = relation.relnamespace
                WHERE namespace.nspname = 'observability'
                  AND relation.relname = 'traces'
                  AND c.conname = 'ck_traces_status'
              ) AS trace_status_constraint
            """
        )
        return await cursor.fetchone()
    finally:
        await connection.close()


def _assert_submission_schema(state: dict[str, object]) -> None:
    assert state["trace_columns"] == {
        "channel": {
            "data_type": "text",
            "udt_name": "text",
            "nullable": True,
        },
        "external_message_id": {
            "data_type": "text",
            "udt_name": "text",
            "nullable": True,
        },
    }
    assert state["job_columns"] == {
        "trace_id": {
            "data_type": "uuid",
            "udt_name": "uuid",
            "nullable": True,
        },
        "result": {
            "data_type": "jsonb",
            "udt_name": "jsonb",
            "nullable": True,
        },
        "finished_at": {
            "data_type": "timestamp with time zone",
            "udt_name": "timestamptz",
            "nullable": True,
        },
        "lease_expires_at": {
            "data_type": "timestamp with time zone",
            "udt_name": "timestamptz",
            "nullable": True,
        },
        "claim_token": {
            "data_type": "uuid",
            "udt_name": "uuid",
            "nullable": True,
        },
    }
    assert state["has_trace_foreign_key"] is True
    assert state["has_trace_index"] is True
    constraint = state["trace_status_constraint"]
    assert constraint is not None
    for status in ("queued", "running", "succeeded", "failed"):
        assert f"'{status}'" in constraint


@pytest.mark.asyncio
async def test_existing_0001_database_upgrades_to_head(test_database_url):
    async with _scratch_database(test_database_url, "upgrade") as database_url:
        await _upgrade(database_url, "0001_bootstrap_runtime")
        at_bootstrap = await _schema_state(database_url)
        assert at_bootstrap == {
            "has_turn_inputs": True,
            "has_claim_token": False,
            "has_settlement_backoff": False,
            "next_attempt_nullable": False,
            "revision": "0001_bootstrap_runtime",
        }
        await _upgrade(database_url, "head")
        assert await _schema_state(database_url) == {
            "has_turn_inputs": True,
            "has_claim_token": True,
            "has_settlement_backoff": True,
            "next_attempt_nullable": True,
            "revision": "0003_turn_submissions",
        }
        _assert_submission_schema(await _submission_schema_state(database_url))


@pytest.mark.asyncio
async def test_fresh_database_upgrades_directly_to_head(test_database_url):
    async with _scratch_database(test_database_url, "fresh") as database_url:
        await _upgrade(database_url, "head")
        assert await _schema_state(database_url) == {
            "has_turn_inputs": True,
            "has_claim_token": True,
            "has_settlement_backoff": True,
            "next_attempt_nullable": True,
            "revision": "0003_turn_submissions",
        }
        _assert_submission_schema(await _submission_schema_state(database_url))


@pytest.mark.asyncio
async def test_older_0001_missing_turn_inputs_converges_to_head(test_database_url):
    async with _scratch_database(test_database_url, "older_drift") as database_url:
        await _upgrade(database_url, "0001_bootstrap_runtime")
        connection = await psycopg.AsyncConnection.connect(database_url)
        try:
            await connection.execute("DROP TABLE runtime.turn_inputs")
            await connection.commit()
        finally:
            await connection.close()
        await _upgrade(database_url, "head")
        assert await _schema_state(database_url) == {
            "has_turn_inputs": True,
            "has_claim_token": True,
            "has_settlement_backoff": True,
            "next_attempt_nullable": True,
            "revision": "0003_turn_submissions",
        }


@pytest.mark.asyncio
async def test_f117_partial_outbox_0001_converges_to_head(test_database_url):
    async with _scratch_database(test_database_url, "f117_drift") as database_url:
        await _upgrade(database_url, "0001_bootstrap_runtime")
        connection = await psycopg.AsyncConnection.connect(database_url)
        try:
            await connection.execute(
                "ALTER TABLE notification.outbox "
                "ALTER COLUMN next_attempt_at DROP NOT NULL, "
                "ADD COLUMN last_http_status integer, "
                "ADD COLUMN lock_owner text, "
                "ADD COLUMN locked_at timestamptz, "
                "ADD COLUMN lease_expires_at timestamptz, "
                "ADD CONSTRAINT ck_outbox_attempts CHECK (attempts >= 0)"
            )
            await connection.commit()
        finally:
            await connection.close()
        await _upgrade(database_url, "head")
        assert await _schema_state(database_url) == {
            "has_turn_inputs": True,
            "has_claim_token": True,
            "has_settlement_backoff": True,
            "next_attempt_nullable": True,
            "revision": "0003_turn_submissions",
        }


@pytest.mark.asyncio
async def test_downgrade_preserves_converged_data_bearing_schema(test_database_url):
    async with _scratch_database(test_database_url, "downgrade") as database_url:
        await _upgrade(database_url, "head")
        await _downgrade(database_url, "0001_bootstrap_runtime")
        assert await _schema_state(database_url) == {
            "has_turn_inputs": True,
            "has_claim_token": True,
            "has_settlement_backoff": True,
            "next_attempt_nullable": True,
            "revision": "0001_bootstrap_runtime",
        }
        await _upgrade(database_url, "head")


@pytest.mark.asyncio
async def test_partial_turn_submission_schema_converges_to_head(test_database_url):
    async with _scratch_database(test_database_url, "submission_drift") as database_url:
        await _upgrade(database_url, "0002_handoff_outbox")
        connection = await psycopg.AsyncConnection.connect(database_url)
        try:
            await connection.execute(
                "ALTER TABLE observability.traces ADD COLUMN channel text; "
                "ALTER TABLE observability.traces "
                "DROP CONSTRAINT ck_traces_status; "
                "ALTER TABLE observability.traces "
                "ADD CONSTRAINT ck_traces_status "
                "CHECK (status IN ('queued', 'running')); "
                "ALTER TABLE runtime.jobs ADD COLUMN result jsonb; "
                "ALTER TABLE runtime.jobs ADD COLUMN trace_id uuid "
                "REFERENCES observability.traces(id) ON DELETE CASCADE"
            )
            await connection.commit()
        finally:
            await connection.close()

        await _upgrade(database_url, "head")
        _assert_submission_schema(await _submission_schema_state(database_url))
