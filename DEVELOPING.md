# Developing Tracewright

For whoever picks this up next. It assumes you have read the README and can run
the demo. Everything here is what the code will not tell you by itself.

---

## Run it

```bash
uv sync --frozen                              # dependencies
uv run --frozen alembic upgrade head          # database schema
uv run --frozen uvicorn --factory agent_flow.runtime:create_runtime_app --reload
uv run --frozen python -m agent_flow.worker --run
```

Or `./run.sh` for the Docker version, which is what the demo runs on.

**One thing that will waste an hour if nobody tells you:** `src/` is baked into
the image, `config/` is bind-mounted. So a code change needs
`docker compose up -d --build app worker`, but a config change only needs
`docker compose restart app worker` — and artifacts load at startup, so a config
change does need that restart.

## The tests

Six directories, and they do not all cost the same.

| Directory | Needs | Time | Run it |
|---|---|---|---|
| `tests/unit` | nothing | ~5s | always |
| `tests/contract` | nothing | in the above | always |
| `tests/browser` | Playwright browsers | ~15s | when you touch `console/` |
| `tests/integration` | a PostgreSQL | ~30s | when you touch `repositories/` or migrations |
| `tests/e2e` | Docker Compose | minutes | before a release |
| `tests/live` | a real model server | minutes, costs tokens | when you change prompts or the gateway |

The everyday command is:

```bash
uv run pytest tests/unit tests/contract -q      # ~300 tests, no services
```

`uv run pytest` with no arguments tries to run all six and will fail on a
machine without Postgres and a model server. That is not a broken checkout.

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

Not omissions — decisions. Reopen them with your eyes open.

| Not built | Why |
|---|---|
| SSE or WebSocket for live traces | The console is a debug tool, polling is not measurably hurting anything, and `EventSource` cannot send an `Authorization` header — it would need a ticket endpoint. |
| A message broker | The job queue is a Postgres table. One less service to run, and the trace store is the same database. |
| Real authentication | Two static demo tokens. `APP_RUNTIME_MODE=production` refuses them, which is the hook for doing this properly. |
| Model config over the API | It carries endpoint URLs and selects credentials; an admin token should not be able to repoint a model role at a server it chooses. |
| Semantic RAG over the customer's words | Retrieval is bounded to catalog source ids on purpose — see the invariant above. Changing this needs a new answer to "how does the classifier not invent a document". |
| Embeddings / pgvector retrieval | Wired but disabled: the demo answers from fixtures, and the code expects 1024 dimensions. |
| CI | Nobody set it up. `tests/unit tests/contract` need no services and would be a five-minute workflow. |

## Where to start reading

1. `pipeline/turn.py` — the whole flow in one file
2. `contracts.py` — the shapes everything passes around
3. `pipeline/evidence.py` — retrieval, and the invariant in action
4. `pipeline/validate.py` — what stops a wrong answer
5. `runtime.py` — how the real objects get wired together

Then pick a trace in the console and follow it through those five files. That is
the fastest way to hold the whole thing in your head.
