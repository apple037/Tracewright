# Live Trace Console — Implementation Review (2026-07-23)

Scope: Tasks 5–11 of
`docs/superpowers/plans/2026-07-23-live-trace-console-implementation.md`
(Tasks 1–4 were already committed). Reviewed for spec coverage, authorization
binding, queue races, secret leakage, retry semantics, model-capability
mismatches, DOM injection, and over-engineering.

Verification at review time: `uv run pytest -q` → **310 passed, 64 skipped**
(only opt-in DB/live-model/CI-browser paths skip); `uv build` succeeds and the
wheel contains every `agent_flow/console` asset; `docker compose config`
resolves without leaking secret values.

## Spec coverage

- **Task 5 — telemetry.** `observability.py` binds a `ContextVar` node context;
  `record_model/record_rag/record_tool` emit fixed allowlisted payloads through
  `sanitize_trace_value`. `summarize_node_result` dispatches by node name and
  returns only the 8 allowlisted keys. Adapters and `TurnPipeline.run_node` are
  instrumented without changing public results; a missing context is a no-op so
  standalone probes create no rows.
- **Task 6 — submission/trace-list/retry APIs.** `InboundMessageService` +
  `POST/GET /api/v1/submissions` + cursor-paginated `GET /api/v1/traces`. Legacy
  `/turns` and manual retry now route through the queue; `api/turns.py` and the
  retry path no longer call `pipeline.run` directly.
- **Task 7 — demo composition root.** `runtime.py` builds real repositories, one
  shared registry + telemetry, mock evidence adapters, and the pipeline;
  `demo_lifespan` opens the pool, probes, installs `AppServices`, and closes once.
  `DemoTokenAuthenticator` uses `hmac.compare_digest`; `ReadinessCheck` replaces
  string-only checks.
- **Tasks 8–10 — console.** Packaged static shell at `/console`, memory-only
  token, allowlisted DOM rendering, bounded polling, inbound simulator, and
  manual-retry lineage — all covered by Playwright tests.
- **Task 11 — docs.** README documents the demo tokens (demo-only), console,
  submission API, queue recovery, the failure-location chain, and the future LINE
  boundary; a doc-contract test guards those topics.

## Findings

No blocking or high-severity findings. All areas below are CONFIRMED against the
committed code and passing tests.

- **Authorization binding (CONFIRMED safe).** Submission and legacy-turn requests
  bind `AuthorizedCustomerContext` from the principal (`bind_customer_context(...,
  None, None)`); the request schema carries no `customer_id`. Reads are scoped by
  tenant *and* customer, so a cross-customer id returns 404 and a cross-customer
  trace list returns `[]` (`test_other_customer_cannot_read_submission`,
  `test_trace_list_is_tenant_and_customer_scoped`).
- **Secret leakage (CONFIRMED safe).** Telemetry never stores model text,
  reasoning, raw tool arguments, or query strings — only fingerprints, token
  counts, durations, and allowlisted decision codes. Readiness reports a generic
  `MODEL_CAPABILITY_FAILED` with no exception body
  (`test_required_probe_failure_marks_models_not_ready_without_content`).
- **DOM injection (CONFIRMED safe).** `render.js` builds every dynamic node with
  `document.createElement` + `textContent` and an explicit `DETAIL_FIELDS`
  allowlist; unknown fields collapse to a static string. No `innerHTML`.
- **Retry semantics (CONFIRMED).** Manual retry keeps all authorization/expiry/
  artifact/lineage checks, then enqueues with `delivery_disposition=
  "review_required"`; the worker maps that to `suppress_handoff`. Lineage stays
  capped at three via `count_retries`.
- **Queue races (CONFIRMED at the logic level).** Claims use `FOR UPDATE SKIP
  LOCKED` + per-claim tokens + leases; `recover_expired_claim` finalizes the
  abandoned trace as `WORKER_LEASE_EXPIRED` and reserves a retry under the same
  root, all in one transaction.

## Observations (non-blocking, accepted)

1. **Legacy `/turns` assurance.** `SubmissionResult` intentionally omits
   `assurance`, so the legacy endpoint reconstructs `TurnResult` and reads
   `pipeline._assurance()` for the metadata. Minor coupling to a private method;
   acceptable because the queue result contract stays free of pipeline internals.
2. **`RuntimeComponents.inventory_probe`.** Added one field beyond the plan's
   listed set so the lifespan can run an injected probe; keeps `build_demo_
   components` side-effect free (probing happens in the lifespan, which is what
   the failure-readiness test exercises).
3. **Test-only inline execution.** `tests/e2e/conftest.py::MemorySubmissions`
   executes the reserved turn inline, mirroring `TurnJobWorker`, so the in-process
   API tests reach terminal state without a worker. Production uses the real
   worker; this double is confined to tests.
4. **Environment-gated coverage.** Postgres submission/repository atomicity,
   the live vLLM capability gate, and CI Chromium runs remain opt-in skips here;
   their logic is exercised by fakes and, for the console, by local Chromium.

## Conclusion

The implementation matches the approved plan with no blocking defects. The full
suite, wheel build, and Compose config all pass; the only non-executed step is
the heavyweight `docker compose build` image build, which is CI/registry
territory and does not affect correctness of the reviewed code.
