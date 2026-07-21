import asyncio
import os

import pytest
import pytest_asyncio

from agent_flow.repositories.postgres import PostgresPool
from agent_flow.repositories.traces import PostgresTraceRepository


@pytest.fixture(scope="session")
def event_loop_policy():
    if os.name == "nt":
        return asyncio.WindowsSelectorEventLoopPolicy()
    return asyncio.DefaultEventLoopPolicy()


@pytest.fixture(scope="session")
def test_database_url() -> str:
    value = os.getenv("TEST_DATABASE_URL")
    if not value:
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
