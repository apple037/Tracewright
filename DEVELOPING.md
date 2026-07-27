# Developing Tracewright

[English](DEVELOPING.md) | [繁體中文](DEVELOPING.zh-TW.md)

For whoever picks this up next. It assumes you have read the README and can run
the demo. Everything here is what the code will not tell you by itself.

---

## Run it without Docker

Needs Python 3.12, [uv](https://docs.astral.sh/uv/), a reachable PostgreSQL, and
a model server. `./run.sh` is the Docker version and what the demo runs on.

```bash
cp .env.example .env      # then edit DATABASE_URL to a host-reachable address
uv sync --frozen
uv run --frozen alembic upgrade head
uv run --frozen python -m agent_flow.seed_demo

export KNOWLEDGE_BASE_URL=http://localhost:8000/mock-kb   # in BOTH terminals
uv run --frozen uvicorn --factory agent_flow.runtime:create_runtime_app --reload
uv run --frozen python -m agent_flow.worker --run         # second terminal
```

`KNOWLEDGE_BASE_URL` matters because the demo knowledge base is served by the app
itself: on Compose that is `http://app:8080/mock-kb`, on the host it is port
8000. Get it wrong and retrieval silently returns nothing. The console is then at
`http://localhost:8000/console/`.

**Rebuild or restart — get this wrong and you will spend an hour debugging a
change that never reached the container.** `src/` is baked into the image,
`config/` is bind-mounted:

| You changed | Run |
|---|---|
| anything under `src/` | `docker compose up -d --build app worker` |
| anything under `config/` | `docker compose restart app worker` |

A config change still needs that restart: artifacts are loaded once, at startup.

`compose.yaml` owns `DATABASE_URL` (it must be the container hostname
`postgres`) and pins `APP_RUNTIME_MODE=demo`. Setting those in `.env` affects
host startup only.

## Dependencies

There is no `requirements.txt`, and adding one would create a second thing to
keep in step with the lock file. Three places, one of them generated:

| Where | What |
|---|---|
| `pyproject.toml` | The 8 runtime dependencies and 4 dev ones, with version ranges. Edit here. |
| `uv.lock` | Every transitive package pinned with hashes — committed, and what `--frozen` installs. Never edit by hand. |
| `uv export --frozen --no-dev` | A pip-compatible list with hashes, on demand, for a machine without uv. |

`uv sync --frozen` fails rather than resolving if the lock and `pyproject.toml`
disagree, so a dependency change means `uv lock` and committing the result.

## The tests

Six directories, and they do not all cost the same.

| Directory | Needs | Time | Run it |
|---|---|---|---|
| `tests/unit` | nothing | ~5s | always |
| `tests/contract` | nothing | in the above | always |
| `tests/browser` | Playwright browsers | ~15s | when you touch `console/` |
| `tests/e2e` | nothing — it drives the pipeline in-process against fakes | ~2s | always |
| `tests/integration` | a PostgreSQL named `*test*` | ~20s | when you touch `repositories/` or migrations |
| `tests/live` | a real model server | minutes, costs tokens | when you change prompts or the gateway |

The everyday command is `make test`, which is the four service-free suites and
exactly what CI's `fast` job runs:

```bash
uv run pytest tests/unit tests/contract tests/browser tests/e2e -q   # 384 tests
```

Run the same set locally as CI does. When those two drift, a local pass stops
being evidence of anything — two failing tests reached `master` that way.

For `tests/integration`, point `TEST_DATABASE_URL` at a database whose name
contains `test`; the destructive cleanup in `conftest.py` refuses anything else.
Without that variable the suite **skips itself**, so set
`REQUIRE_DB_INTEGRATION=true` when you mean to run it and want a missing
configuration to fail rather than pass quietly.

`uv run pytest` with no arguments collects all six and will fail on a machine
without Postgres and a model server. That is not a broken checkout.

**If you add a browser test, use the `authenticate()` helper and do not
reimplement it.** Logging in restores the chat, loads the traces and then puts
the cursor in the composer, several awaits after the click returns. A test that
starts driving the page before that lands gets its focus stolen mid-test. The
helper waits for the composer to be focused, which is the app's own signal that
login has settled; skipping that wait is what made two tests flaky at about one
run in four.

## How a message becomes a reply

```
POST /api/v1/submissions
   └─ app writes a row to the jobs table, returns 202 immediately
        └─ worker leases the job, runs the 8-node pipeline, writes spans
             └─ console polls /submissions/{id} until terminal
```

Two processes, one database, no message broker. **This shapes more of the code
than anything else** — see "Two processes" below.

The pipeline is `src/agent_flow/pipeline/turn.py`, and it is the file to read
first. It calls, in order: `classify` → `risk` → `evidence` (plan, collect,
validate) → `respond` (strategy, generate) → `validate` → repair or hand off.

## The invariant everything else serves

**The assistant may only state facts that were retrieved this turn.**

Three mechanisms enforce it, and if you change one you should understand the
other two:

1. **The classifier may only name a source it saw in the catalog**
   (`adapters/evidence.py::catalog`). It cannot invent a document id. This is
   why `plan_evidence` queries by `source_id` and not by the customer's words.
2. **Deterministic hard failures** (`pipeline/validate.py::_hard_failures`) —
   a citation that does not match retrieved evidence, a price with no source, a
   delivery promise, an action commitment. These are regex-level checks and they
   run before any model.
3. **One AI judge** reads the draft against the evidence
   (`config/prompts/response_judge.v1.yaml`).

Failing 2 or 3 means repair, then handoff. A handoff is a **success** of this
design, not an error — the alternative was making something up.

The system labels its own confidence `reduced_assurance` because one judge is
not two. `ASSURANCE_MODE=dual_judge` is the stricter path.

## Two processes, one database

The API runs in `app`. The pipeline runs in `worker`. They share only Postgres.

Every "live edit" feature has to cross that boundary, and **this is where the
bugs are**. A concrete one, fixed: the Tune panel wrote a prompt override in
`app`, and `worker` held the prompts it booted with, so console edits were
silently ignored while the API reported them applied. Both
`RuntimeConfigService` and `KnowledgeSources` now reload when their files change
on disk.

**If you add another live-editable thing, it needs the same treatment, and a
test that exercises two service instances over one directory.** Every existing
test that builds one object in one process will pass while the feature is
broken.

## Where knowledge actually comes from

The main corpus lives in an **external knowledge base**, reached over HTTP:
`catalog_url` lists what it holds, `document_url` fetches one. That list is what
the classifier is shown, and it may only name a source it saw there — so the
knowledge base decides what exists and the invariant above still holds.

`api/mock_kb.py` is a stand-in serving the committed demo corpus over that same
interface, so the demo needs no external service. It is a fake, not a feature:
point `KNOWLEDGE_BASE_URL` at a real knowledge base and delete the router.

Anything the knowledge base should not hold — documents bound to one customer,
entries a tuner edits from the console — stays in a local `fixture` source.
Expiry and per-customer scoping belong to whoever owns the corpus, so the mock
implements both; a real knowledge base is expected to do the same.

## Extending it

Each of these is a registry plus a config file. None of them touches the
pipeline.

| To add | Register in | Declared in |
|---|---|---|
| A knowledge backend (vector DB, another API shape) | `adapters/knowledge.py::_BUILDERS` | `config/knowledge.yaml` — `fixture` and `http` ship |
| A tool backend (another ERP/CRM shape) | `adapters/tools.py::_BUILDERS` | `config/tools.yaml` — `fixture` and `http` ship |
| A model provider | `config/models*.yaml` — profiles are data | — |
| A channel (LINE, web widget) | translate the webhook into `POST /api/v1/submissions` | — |

The channel case is worth stressing: the submission API is channel-neutral and
`session_id` is whatever chat id the channel supplies. A LINE adapter needs no
pipeline change at all.

**A new pipeline node** is the one thing that is not config. You would add it to
`pipeline/turn.py`, give it a prompt artifact in `config/prompts/`, and add its
output contract to `pipeline/model_outputs.py`. Nodes are ordinary async
functions taking typed inputs; there is no plugin system and deliberately so.

## Conventions worth keeping

- **Every model output is a Pydantic contract** (`contracts.py`,
  `pipeline/model_outputs.py`). No loose dicts crossing a node boundary. Small
  models return nonsense often enough that this is load-bearing.
- **Errors surface as safe codes, never raw internals.** `WORKER_LEASE_EXPIRED`,
  `TOOL_TIMEOUT`, `MODEL_CAPABILITY_FAILED`. Readiness output must never carry
  model output or an exception body — an upstream URL can hold a key in its
  query string.
- **Scope comes from the token, never from the body.** `bind_customer_context`
  is the only way to get an `AuthorizedCustomerContext`.
- **Comments explain why, not what.** Most comments in this repo record a bug
  that was actually hit. If you delete one, make sure the bug cannot come back.
- **Prompts are versioned artifacts.** Change the text, bump `version`; the
  checksum lands on the span so a trace always identifies what produced it.

## Things deliberately not built

Not omissions — decisions. Reopen them with your eyes open. Where the approved
design in `docs/superpowers/specs/` says something the code no longer does,
[docs/decisions.md](docs/decisions.md) records which won and why; trust the spec
everywhere else.

| Not built | Why |
|---|---|
| SSE or WebSocket for live traces | The console is a debug tool, polling is not measurably hurting anything, and `EventSource` cannot send an `Authorization` header — it would need a ticket endpoint. |
| A message broker | The job queue is a Postgres table. One less service to run, and the trace store is the same database. |
| Real authentication | Two static demo tokens. `APP_RUNTIME_MODE=production` refuses them, which is the hook for doing this properly. |
| Model config over the API | It carries endpoint URLs and selects credentials; an admin token should not be able to repoint a model role at a server it chooses. |
| Semantic RAG over the customer's words | Retrieval is bounded to catalog source ids on purpose — see the invariant above. Changing this needs a new answer to "how does the classifier not invent a document". |
| Embeddings / pgvector retrieval | Wired but disabled: the demo answers from fixtures, and the code expects 1024 dimensions. |
| CI on a real deployment | `.github/workflows/ci.yml` runs the tests and a secret scan. It does not build or publish an image, because there is nowhere to deploy it to yet. |

## Operational details worth knowing

**The API.** Scope is always bound from the bearer token, so no request body
carries a `customer_id`.

| Endpoint | Does |
|---|---|
| `POST /api/v1/submissions` | Queue a turn: `{channel, external_message_id, session_id, text, idempotency_key}`. Returns 202. |
| `GET /api/v1/submissions/{id}` | Poll until terminal. |
| `GET /api/v1/traces/{id}` | The whole trace, node by node. |
| `GET /api/v1/sessions` | Chats this token may see, newest first. |
| `GET /api/v1/sessions/{id}/messages` | Replay a transcript — the console's reload path. |
| `GET /api/v1/sessions/{id}/memory` | The windowed slice the pipeline will load next turn, not the whole transcript (admin). |
| `DELETE /api/v1/sessions/{id}/memory` | Forget this session — a soft delete that hides the transcript too (admin). |
| `POST /api/v1/sessions/{id}/memory/rebuild` | Undo that, and re-derive exchanges that were never stored (admin). |
| `GET/PUT/DELETE /api/v1/config/...` | Live prompts, personas, knowledge (admin). |

`/docs` is the full interactive reference, with an Authorize button.

**Memory** is per `session_id`, role-tagged, and windowed to `HISTORY_TURNS`
(default 8). Unbounded history used to exceed the classifier's 100-message cap
and fail the turn outright. Handed-off turns are kept too, so a customer who
rephrases after a handoff does not start from zero.

**Every failure surfaces as a safe error code**, never raw internals: a stalled lease finishes
the trace with `WORKER_LEASE_EXPIRED` and reserves a retry trace; a tool timeout
is `TOOL_TIMEOUT` on component `order_api`. The full chain is
`readiness check → model role → probe stage → trace node → component → operation
→ error code → retry disposition`.

**Model roles.** Each pipeline step is a role — `dialogue_classifier`,
`strategy_advisor`, `response_generator`, `response_judge`, `embedding`. The
**role names are stable**; the profile and model behind each one are yours to
replace in the models file. `structured_output` per profile decides how JSON is
enforced: Ollama's `/v1` accepts OpenAI's `json_schema` field and then ignores
it, so Ollama profiles must use `json_object`; vLLM, TGI and the OpenAI API
support `json_schema`. The schema is repeated in the system prompt either way.

Check the endpoint itself rather than trusting the Ollama CLI's implicit
localhost — `./run.sh` runs this for you at startup:

```bash
curl "${REMOTE_MODEL_BASE_URL%/v1}/api/tags"
```

**The two demo tokens are demo only** and must never gate a real deployment.
`APP_RUNTIME_MODE=demo` enables that authenticator; `production` rejects it.

**Inspect a stuck handoff** for one authorized tenant, never across tenants:

```sql
select tenant_id, id, status, attempts, last_error_code, last_http_status, next_attempt_at
from notification.outbox
where tenant_id = '<tenant-id>' and status = 'failed'
order by created_at desc;
```

## Before you hand this to someone else

- [ ] `.env` is still git-ignored, and no token, key or internal address leaked
      into a tracked file, screenshot or log.
- [ ] The next maintainer knows where the model server is and which model names
      `MODEL_CONFIG_PATH` selects — and can actually reach that file (it may be a
      git-ignored local one).
- [ ] `docker compose config --quiet` is clean, the demo starts from an empty
      database, and `/health/ready` passes.
- [ ] One real message goes end to end with an admin token — API, worker, model,
      knowledge, tool, trace.
- [ ] Say whether `config/overrides.json` exists and which console edits still
      need writing back into YAML.
- [ ] Say that `./run.sh reset` and `docker compose down -v` are irreversible.

## Where to start reading

1. `pipeline/turn.py` — the whole flow in one file
2. `contracts.py` — the shapes everything passes around
3. `pipeline/evidence.py` — retrieval, and the invariant in action
4. `pipeline/validate.py` — what stops a wrong answer
5. `runtime.py` — how the real objects get wired together

Then pick a trace in the console and follow it through those five files. That is
the fastest way to hold the whole thing in your head.
