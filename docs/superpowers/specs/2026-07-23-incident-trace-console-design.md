# Incident Trace Console Design

## Goal

Deliver a runnable incident-first demo console at `/console/` that makes the
agent pipeline, decisions, tool activity, failures, and retry lineage easy to
inspect. The first milestone runs completely from committed mock traces so the
missing production composition root does not block the UI.

## Scope

The first version provides:

- A dark, high-contrast operational console.
- A horizontal pipeline with details expanding downward.
- Failed nodes expanded by default.
- Trace selection, status filtering, attempt selection, and mock event polling.
- Decision, tool-use, model, artifact, retry, and exact error-location details.
- A demo-only manual retry that creates an in-memory retry attempt.
- Four committed scenarios: success, tool timeout, high-risk handoff, and
  manual-retry lineage.

The first version does not provide:

- Production authentication or token storage.
- A working production `HttpTraceSource`.
- Raw model chain-of-thought or exception bodies.
- WebSockets, chart libraries, a frontend framework, or a Node build chain.
- The missing backend runtime composition root.

## Delivery Architecture

FastAPI mounts packaged static assets at `/console`. Assets live under the
Python package so source installs, wheels, and Docker images all resolve the
same path. The console uses native HTML, CSS, and JavaScript ES modules.

The JavaScript boundary consists of:

- `data-source.js`: the `TraceSource` contract and `MockTraceSource`.
- `state.js`: selected trace/attempt, filters, expansion state, event cursor,
  polling state, and retry lineage.
- `render.js`: safe DOM construction and accessible interaction rendering.
- `app.js`: initialization, polling, selection, filtering, and mock retry
  orchestration.

The reserved `HttpTraceSource` contract uses the same method names and response
shape but is not activated in this milestone:

```text
listTraces()
getTrace(traceId)
getEvents(traceId, afterSequence)
retry(traceId, reason)
```

## Core Runtime Flow

1. The browser opens `/console/`.
2. `MockTraceSource` loads the committed trace index and selected trace.
3. State derives the fixed pipeline order and attempt lineage.
4. The renderer creates one horizontal column per node.
5. Error nodes start expanded; other nodes expand by click or keyboard.
6. Polling requests events after the highest observed sequence and merges them
   without duplication.
7. Manual retry validates terminal status, a non-empty reason, and a maximum of
   three retries, then appends a demo attempt linked to the original root.

The page always displays a visible `Mock Data / Demo Only` badge. A mock retry
never calls the backend.

## Page Layout

The header contains the product title, mock badge, trace selector,
status filters, auto-refresh control, and last-updated/stale state.

The main incident strip contains exact failure stage, component, operation,
error code, and retry disposition when a trace failed or handed off.

The pipeline uses a horizontally scrollable row. Each node is a vertical
column:

- A compact status card stays at the top.
- Connectors align across the status-card row.
- Clicking the card expands its detail panel downward within the column.
- Multiple nodes may remain open.
- Failed nodes open on initial render.

The footer contains attempt tabs, root/retry relationships, review-required
state, and the manual-retry action.

## Visual System

The console uses dark gray layered surfaces rather than pure black:

- Page background: near-black blue-gray.
- Node surfaces: lighter charcoal with visible borders.
- Primary text: off-white; secondary text: cool gray.
- Success: green.
- Running/pending: blue.
- Warning/handoff: amber.
- Failure: red.

Focus rings, status icons, labels, and text accompany every color. The flow
remains horizontally scrollable on narrow screens; text never becomes black on
dark surfaces.

## Trace and Event Presentation

Expanded nodes may show only bounded structured fields:

- `decision_summary` and `reason_codes`.
- Model role/profile, duration, token usage, and capability metadata.
- Tool name, authorized argument metadata, result metadata, and freshness.
- Retry attempt, disposition, and lineage identifiers.
- Error code, failure stage, component, and operation.
- Artifact IDs, semantic versions, and checksums.
- Event sequence and timestamps.

The console never displays raw conversation text, Authorization/Cookie/API-key
values, database URLs, raw exceptions, hidden reasoning, or chain-of-thought.
Unknown fields are ignored rather than rendered automatically.

## Mock Data Contract

Each mock trace has:

- Stable trace, root-trace, tenant, customer, session, and attempt identifiers.
- Assurance mode and judge roles.
- Ordered node summaries with status, timing, structured detail allowlists, and
  optional error location.
- An event stream with unique, strictly increasing sequence numbers.
- A terminal or active trace status.
- Retry lineage and review-required metadata where applicable.

The four scenarios cover:

1. Successful reduced-assurance response.
2. `order.lookup` timeout identified as `order_api / order.lookup`.
3. High-risk handoff ending at `risk_precheck` before evidence collection.
4. Root trace plus manual retry attempt with immutable lineage.

## Error Handling

- Initial-load failure shows an error banner and `Retry Load`.
- Polling failure retains the last successful view, marks it stale, and applies
  bounded backoff.
- Unknown nodes/events render as a generic safe summary without crashing.
- Malformed payloads produce a schema warning and are not rendered.
- Manual retry is disabled for non-terminal traces, missing reasons, or lineage
  beyond three retries.

Dynamic values are inserted with DOM text nodes or `textContent`; event data is
never assigned to `innerHTML`.

## Packaging and Server Integration

FastAPI mounts `/console` after API routers without changing existing API or
health paths. Static files are included in the Python wheel and Docker build.
`/console/` is same-origin with the future `/api/v1` adapter, so no CORS or
browser token persistence is introduced.

The existing Compose `app` service exposes the console on port `8080`. No new
Compose service or runtime dependency is required.

## Verification

Automated tests verify:

- `/console/` and every declared asset return the correct content type.
- Missing assets and traversal attempts do not expose filesystem content.
- All four fixtures satisfy identifiers, node order, event sequence, lineage,
  terminal status, and error-location contracts.
- Fixtures contain none of the prohibited raw or secret fields.
- Existing health and API routes remain unchanged.

Browser verification covers:

- Horizontal flow and narrow-screen scrolling.
- Downward multi-node expansion.
- Failed-node default expansion.
- Trace and attempt switching.
- Status filtering and polling cursor behavior.
- Demo-only manual retry and lineage rendering.
- Keyboard navigation, focus visibility, and no browser-console errors.

## Delivery Order

1. Package and serve an empty console shell.
2. Load and render committed mock traces.
3. Add node expansion, filters, polling, and error handling.
4. Add attempt lineage and demo manual retry.
5. Complete browser, packaging, Docker, accessibility, and regression checks.

The milestone is complete only when the entire mock core flow works from the
running Compose app at `/console/`.
