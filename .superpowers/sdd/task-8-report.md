# Task 8 Implementation Report

## Outcome

Implemented the fixed `TurnPipeline`, explicit zero-argument `run_node` call sites,
typed node spans/events, causal failure selection, safe risk/evidence/validation
handoffs, one bounded repair, reduced-assurance bootstrap mode, immutable retry
snapshot loading, and controller-owned artifact-reference tracing.

## TDD Evidence

### RED

- `uv run pytest tests/unit/pipeline/test_turn.py tests/e2e/test_turn_pipeline.py tests/e2e/test_failure_locations.py -v`
  - Failed during collection with `ModuleNotFoundError: agent_flow.pipeline.turn`.
- `uv run pytest tests/unit/pipeline/test_turn.py::test_run_node_rejects_prompt_bodies_from_trace_metadata -q`
  - Failed because unbounded `full_prompt` metadata was initially accepted.
- `uv run pytest tests/unit/pipeline/test_turn.py::test_turn_pipeline_is_exported_from_pipeline_package -q`
  - Failed with `ImportError` before the package export was added.

### GREEN

- Focused: `uv run pytest tests/unit/pipeline/test_turn.py tests/e2e/test_turn_pipeline.py tests/e2e/test_failure_locations.py -v`
  - 9 passed.
- Full non-live: `uv run pytest tests/unit tests/contract tests/e2e -q`
  - 157 passed; warnings are existing Python 3.14 `pytest-asyncio` deprecations.
- Compile: `uv run python -m compileall -q src tests`
  - Exit 0.

## Files

- `src/agent_flow/pipeline/turn.py`
- `src/agent_flow/pipeline/__init__.py`
- `src/agent_flow/contracts.py`
- `src/agent_flow/repositories/traces.py`
- `tests/unit/pipeline/conftest.py`
- `tests/unit/pipeline/test_turn.py`
- `tests/e2e/conftest.py`
- `tests/e2e/test_turn_pipeline.py`
- `tests/e2e/test_failure_locations.py`

Base SHA: `49a9a06`. Task SHA: the commit containing this report, with message
`feat: add fixed turn pipeline with exact tracing`.

## Self-review

- Every `run_node` operation is a zero-argument closure with arguments bound at
  the controller call site; no name-based dispatch exists.
- `context_loader` is first and records only immutable artifact references.
- Trace metadata is allow-listed and artifact-reference validated; prompt and
  persona bodies and model reasoning are not traced.
- Nested concurrent failures are unwrapped to the causal `AgentError`; only its
  event becomes `primary_failure_event_id`.
- Retry snapshots are rebound byte-for-byte to each new trace, including
  retry-of-retry lineage.
- High-risk and insufficient-evidence paths enqueue handoff without generation.
- A second failed validation produces `VALIDATION_EXHAUSTED`, and no failing
  assistant draft is persisted.
- Existing `TurnResult` remains the single result contract; nullable `text` and
  compatibility accessors express suppressed handoff replies without a parallel
  result type.

## Concerns

- Test output contains upstream `pytest-asyncio` deprecation warnings on Python
  3.14; there are no test failures.
- Trace finalization plus conversation persistence, and handoff enqueue plus
  trace finalization, cross repository boundaries. Production-grade atomicity
  requires a later shared transaction/outbox design; handoff calls include
  `trace_id` as their stable idempotency key.
