import asyncio
import os
import re
from collections.abc import Mapping, Sequence

import pytest
import pytest_asyncio

from agent_flow.repositories.postgres import PostgresPool
from agent_flow.repositories.rag import RagRepository
from agent_flow.repositories.traces import PostgresTraceRepository


RAG_PYTEST_OWNER = "agent-flow-task6"


def _is_unambiguous_test_database(name: str) -> bool:
    if re.fullmatch(r"[A-Za-z0-9]+(?:[_-][A-Za-z0-9]+)*", name) is None:
        return False
    return "test" in re.split(r"[_-]", name.lower())


async def require_unambiguous_test_database(pool: PostgresPool) -> str:
    async with pool.connection() as connection:
        cursor = await connection.execute("SELECT current_database() AS name")
        name = (await cursor.fetchone())["name"]
    if not _is_unambiguous_test_database(name):
        raise RuntimeError("destructive cleanup is restricted to unambiguous test databases")
    return name


def database_integration_required(
    arguments: Sequence[str], environ: Mapping[str, str]
) -> bool:
    required = environ.get("REQUIRE_DB_INTEGRATION", "").lower()
    if required in {"1", "true", "yes"}:
        return True
    for argument in arguments:
        if argument.startswith("-"):
            continue
        normalized = argument.replace("\\", "/").lstrip("./")
        if normalized.startswith("tests/integration") or "/tests/integration" in normalized:
            return True
    return False


@pytest.fixture
def database_requirement_checker():
    return database_integration_required


@pytest.fixture(scope="session")
def event_loop_policy():
    if os.name == "nt":
        return asyncio.WindowsSelectorEventLoopPolicy()
    return asyncio.DefaultEventLoopPolicy()


@pytest.fixture(scope="session")
def test_database_url(request) -> str:
    value = os.getenv("TEST_DATABASE_URL")
    if not value:
        if database_integration_required(request.config.invocation_params.args, os.environ):
            pytest.fail(
                "database integration was explicitly requested but "
                "TEST_DATABASE_URL is not configured"
            )
        pytest.skip("TEST_DATABASE_URL is not configured")
    return value


@pytest_asyncio.fixture
async def postgres_pool(test_database_url: str):
    pool = PostgresPool(test_database_url)
    await pool.open()
    yield pool
    await pool.close()


@pytest_asyncio.fixture
async def trace_repository(postgres_pool: PostgresPool):
    repository = PostgresTraceRepository(postgres_pool)
    await repository.clear_test_data()
    yield repository
    await repository.clear_test_data()


async def _clear_rag_test_data(pool: PostgresPool) -> None:
    await require_unambiguous_test_database(pool)
    async with pool.connection() as connection:
        await connection.execute(
            "DELETE FROM rag.documents "
            "WHERE access_metadata ->> 'pytest_owner' = %s",
            (RAG_PYTEST_OWNER,),
        )


@pytest_asyncio.fixture
async def rag_repository(postgres_pool: PostgresPool):
    repository = RagRepository(postgres_pool)
    await _clear_rag_test_data(postgres_pool)
    yield repository
    await _clear_rag_test_data(postgres_pool)
