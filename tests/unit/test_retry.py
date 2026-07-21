import pytest

from agent_flow.errors import AgentError
from agent_flow.retry import RetryPolicy, run_with_retry


@pytest.mark.asyncio
async def test_503_retries_but_validation_failure_does_not():
    attempts = 0

    async def transient_operation():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise AgentError.dependency("MODEL_503", retryable=True)
        return "ok"

    result, records = await run_with_retry(
        transient_operation, RetryPolicy(max_attempts=2, base_delay_ms=1)
    )

    assert result == "ok"
    assert [record.outcome for record in records] == ["failed", "completed"]
    assert [record.error_code for record in records] == ["MODEL_503", None]


@pytest.mark.asyncio
async def test_policy_error_is_not_retried():
    attempts = 0

    async def operation():
        nonlocal attempts
        attempts += 1
        raise AgentError.validation("UNSUPPORTED_CLAIM")

    with pytest.raises(AgentError, match="The request could not be completed"):
        await run_with_retry(
            operation, RetryPolicy(max_attempts=3, base_delay_ms=1)
        )

    assert attempts == 1


@pytest.mark.asyncio
async def test_undeclared_exception_is_not_retried():
    attempts = 0

    async def operation():
        nonlocal attempts
        attempts += 1
        raise RuntimeError("programming failure")

    with pytest.raises(RuntimeError, match="programming failure"):
        await run_with_retry(
            operation, RetryPolicy(max_attempts=3, base_delay_ms=1)
        )

    assert attempts == 1
