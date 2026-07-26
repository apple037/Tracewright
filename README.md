# Tracewright

**A customer-service AI that answers safely — and shows its work.**

Tracewright takes a customer message, runs it through a chain of small AI steps
(understand → check risk → look up facts → write reply → fact-check the reply),
and then lets an operator **watch every step happen live** in a web console. If
the AI isn't sure, or a message looks risky, it hands off to a human instead of
guessing.

The headline feature is the word in the name: **traces**. Every message becomes
a fully recorded, click-through trace so you can see exactly *why* the AI said
what it said — which is normally the scariest black box in an AI product.

> ⚠️ **This is a demo / bootstrap, not a locked-down production system.** It uses
> small open models and simple demo login tokens. Great for learning, demos, and
> building on top of — not for handling real customers unattended. See
> [Safety & limits](#safety--limits).

---

## What it does, in plain words

Imagine a support chat. A customer types *"Where's my refund?"*. Behind the
scenes Tracewright runs a small assembly line:

| Step | Node name | What it decides |
|------|-----------|-----------------|
| 1. Understand | `dialogue_classifier` | What does the customer want? What's their mood? Is this just chit-chat? |
| 2. Safety check | `risk_precheck` | Is this dangerous or sensitive? If so, **stop and hand to a human**. |
| 3. Plan facts | `evidence_planner` | Do we need to look anything up (order status, policy)? |
| 4. Gather facts | `evidence_collector` | Fetch those facts from the knowledge base / tools. |
| 5. Verify facts | `evidence_validator` | Only keep facts we actually asked for. |
| 6. Pick a style | `strategy_selector` | How should we answer — brief, business-first, supportive? |
| 7. Write reply | `response_generator` | Draft the actual answer, citing its sources. |
| 8. Fact-check | `response_validator` | A second AI checks the draft is grounded and safe. If not → repair or hand off. |

Every one of those steps is recorded as a **trace** with timing, decisions, and
(for admins) the model's raw reasoning. That's what you see in the console.

It also **remembers the conversation**. Step 1 and step 7 both receive the recent
exchanges of the same session, tagged with who said what, so "how long will that
take?" resolves against what was already discussed instead of starting over.

### Why "grounded" matters

The AI is only allowed to state facts it actually retrieved. It can't invent a
refund policy — if it has no evidence, it shouldn't cite one. Step 5 and step 8
exist to enforce that. (A real bug we fixed: a plain "good morning" was pulling
the refund policy into the answer — the pipeline now correctly retrieves nothing
for chit-chat.)

---

## Quick start

You need **Docker Desktop** and a running AI model server (Ollama, vLLM, or
anything OpenAI-compatible).

```bash
./run.sh
```

The first run creates `.env` and stops to ask you for two things: a login token
and your model server's address. Fill them in, run `./run.sh` again, and it
builds, starts, seeds sample data, and prints the URL.

Then open **http://localhost:8080/console/** and paste your **admin** token.

```bash
./run.sh logs     # see what it is doing
./run.sh stop     # stop it
./run.sh reset    # stop and wipe the database
make restart      # pick up edits to config/*.yaml
```

> ⏳ Replies take real time — a 27B model on one GPU is roughly 30–90 seconds per
> message, because a turn makes several model calls. Watch the steps fill in.

---

## Changing how it behaves

Four files, no Python. See **[TUNING.md](TUNING.md)** for the detail.

| I want to change… | Edit |
|---|---|
| How it **sounds** | `config/personas/*.yaml` |
| What each step **decides** | `config/prompts/*.yaml` |
| **Which model** does what | `config/models.yaml` |
| Addresses, passwords, memory length | `.env` |
| What the AI **knows** | `config/demo/rag.json` |

Or click **Tune** in the console with an admin token: it shows every step's
current instructions, the voice, and which model runs each step — and lets you
edit the instructions live, applied to the very next message.

---

## Using the console

```
┌───────────────┬──────────────────────┬─────────────────────┐
│  Turns        │   Conversation       │  What happened      │
│  (this        │   (type here —       │  this turn          │
│   conversation│    the main event)   │  (steps fill in     │
│   auto-       │                      │   live; click one   │
│   refreshes)  │                      │   for its decisions)│
└───────────────┴──────────────────────┴─────────────────────┘
```

1. **Type a message** and press Send. Things worth trying, and what each shows:

   | Say | What happens |
   |---|---|
   | `good morning` | Answers, retrieves **nothing**, cites nothing |
   | `where is my order order-1?` | Looks the order up and cites the tool result |
   | `is it still on the way?` | Reuses the order from the previous turn — **memory** |
   | `how long do refunds take?` | Answers from the knowledge base with a citation |
   | `where is my refund?` | **Hands off to a human** — there is no verified refund source, so it refuses to guess |

2. Its trace **auto-selects** and **streams live** as each step runs.
3. Click any step to expand what it decided.
4. **Refresh the page**: the conversation is still there. **New conversation**
   starts a clean one.
5. **EN / 中文** switches language, **◐** switches light/dark, **Retry trace**
   re-runs a finished turn.

**Admin vs customer token:**
- **Admin** sees everything: real input/output, the model's raw chain-of-thought
  (collapsible, labeled per step), and the Tune panel.
- **Customer** sees only the chat — no internal reasoning.

---

## Tech stack

| Layer | Choice | Why |
|-------|--------|-----|
| Language | **Python 3.12** | The whole backend. |
| Web API | **FastAPI** | Serves the API + the console. |
| Data models | **Pydantic 2** | Strict, typed data at every step (no loose dicts). |
| Database | **PostgreSQL 16 + pgvector** | Stores traces, jobs, and the searchable knowledge base. |
| DB access | **psycopg 3** + **Alembic** | Queries + versioned schema migrations. |
| AI models | Any **OpenAI-compatible** server (Ollama, vLLM, …) | The actual language models; swappable in `config/models.yaml`. |
| Frontend | **Plain HTML/CSS/JavaScript** (no framework) | The console — nothing to build, just static files. |
| Packaging | **uv** + **hatchling** | Fast installs, reproducible builds. |
| Containers | **Docker Compose** | One command runs DB + API + worker. |
| Tests | **pytest** + **Playwright** | Unit, integration, and browser tests. |

**No message queue, no Redis, no Kafka.** The job queue is just a
PostgreSQL table — simpler to run and reason about.

---

## How it's built (system structure)

```
Customer message
      │
      ▼
┌─────────────┐   writes a job    ┌──────────────┐   picks up job   ┌──────────────┐
│  FastAPI    │ ────────────────▶ │  PostgreSQL  │ ───────────────▶ │   Worker     │
│  (app)      │                   │  jobs +      │                  │  runs the 8- │
│  serves API │ ◀──────────────── │  traces      │ ◀─────────────── │  step pipeline│
│  + console  │   reads traces    │  (pgvector)  │   writes traces  │  calls models │
└─────────────┘                   └──────────────┘                  └──────────────┘
      │                                                                    │
      ▼                                                                    ▼
  Browser console                                                    AI models
  (live traces)                                          (any OpenAI-compatible server)
```

**Two processes** share one database:
- **`app`** — the web server. Takes messages in, serves the console, reads traces
  out. Does *not* call the AI itself.
- **`worker`** — the background engine. Pulls queued jobs, runs the pipeline,
  writes every step as a trace.

### Where things live in the code

```
src/agent_flow/
├── main.py            # web app wiring; serves the console
├── runtime.py         # the demo "composition root" — plugs everything together
├── worker.py          # background job runner
├── contracts.py       # the typed data shapes used everywhere
├── observability.py   # how traces/events/reasoning get recorded
├── auth.py            # demo bearer-token login
├── api/               # HTTP endpoints (submissions, sessions, traces, config)
├── runtime_config.py  # live view of the editable config; backs the Tune panel
├── pipeline/          # THE BRAIN — the 8 steps, one file each:
│   ├── classify.py    #   understand the message
│   ├── risk.py        #   safety check
│   ├── evidence.py    #   plan / gather / verify facts
│   ├── respond.py     #   pick style, write reply, repair
│   ├── validate.py    #   fact-check the reply
│   ├── model_outputs.py #  strict shapes for what models may return
│   └── turn.py        #   the conductor that runs steps 1→8 in order
├── adapters/          # talk to the outside: models, RAG search, tools
├── repositories/      # read/write the database
└── console/           # the web UI (HTML/CSS/JS, i18n EN + 中文)

config/                # editable settings, prompts, demo knowledge base
├── models.yaml        # which model powers each step
├── prompts/           # the instructions given to each AI step
├── personas/          # how the assistant sounds
└── demo/rag.json      # the demo knowledge base (facts the AI may cite)
```

See **[TUNING.md](TUNING.md)** — you rarely need to touch Python.

---

## Running without Docker (developers)

If you'd rather run the pieces by hand (needs Python 3.12 + [uv](https://docs.astral.sh/uv/)):

```bash
uv sync --frozen                              # install dependencies
uv run --frozen alembic upgrade head          # set up the database
uv run --frozen uvicorn --factory agent_flow.runtime:create_runtime_app --reload  # API
uv run --frozen python -m agent_flow.worker --run      # start the worker
uv run pytest -q                              # run the tests
```

Health checks: `http://localhost:8000/health/live` (alive) and
`/health/ready` (all models verified).

---

## Safety & limits

- **Demo login only.** The console uses two static tokens set in `.env`. This is
  fine for a demo; a real deployment needs proper authentication
  (`APP_RUNTIME_MODE=production` deliberately refuses the demo tokens).
- **Small models = variable quality.** A small local model can produce weak or
  off replies on open chit-chat. That's expected; a larger model improves it.
- **Console edits are live.** The Tune panel changes behaviour for the next
  message without a restart or a code review. Only admin tokens can reach it.
- **"Reduced assurance."** Fact-checking uses deterministic rules plus one AI
  judge, so the system labels its own confidence as `reduced_assurance` and is
  **not** approved to run unattended in production.
- **Admin reasoning is stored.** Admin tokens can view the models' raw
  chain-of-thought; it's recorded in the trace and filtered out for everyone
  else. Turn it off if that's not acceptable for your use.
- **Never commit secrets.** `.env`, real tokens, and API keys stay local. The
  repo is regularly checked to ensure none leak in.

---

## Operations reference

Detail for whoever runs or extends this. The friendly guide above is enough for
a demo; this section is the operational contract.

### Models & configuration

- Each pipeline step is a **model role** (`dialogue_classifier`,
  `strategy_advisor`, `response_generator`, `response_judge`, `embedding`, …).
  the **role names are stable**; the *profile* and *model* names behind them are
  replaceable in `config/models.yaml`. Swap models there, not in code.
- The default config points every chat role at one model on one
  OpenAI-compatible endpoint (`REMOTE_MODEL_BASE_URL`). Split roles across
  profiles when you want a bigger model for the final reply only.
- `structured_output` per profile decides how JSON is enforced. Ollama's `/v1`
  accepts OpenAI's `json_schema` field and **ignores** it, so Ollama profiles
  must use `json_object`; the schema is always repeated in the system prompt so
  a backend without grammar enforcement still complies.
- The `embedding` role is disabled by default: the demo answers from a fixture,
  and the code requires a 1024-dimension embedding model.
- Verify the endpoint directly rather than trusting the Ollama CLI's implicit
  localhost: `curl "${REMOTE_MODEL_BASE_URL%/v1}/api/tags"`. `./run.sh` runs this
  check for you at startup.

### Conversation memory

- History is per `session_id`, role-tagged, and windowed to `HISTORY_TURNS`
  (default 8) most recent exchanges — unbounded history used to exceed the
  classifier's 100-message cap and fail the turn outright.
- Handed-off turns are recorded too, so a customer who triggers a handoff and
  then rephrases does not start from zero context.
- `GET /api/v1/sessions/{session_id}/messages` replays the visible transcript,
  scoped to the authenticated tenant and customer. The console uses it to
  restore the chat after a reload.

### Editing prompts at runtime

- `GET /api/v1/config` (admin) reports the live prompts, personas, model roles
  and settings. `PUT`/`DELETE` on `/api/v1/config/prompts/{node}` and
  `/api/v1/config/personas/{artifact_id}` set and clear overrides.
- Overrides live in `config/overrides.json` (git-ignored) and never rewrite the
  YAML, so the commented config files stay intact and revert always works.
- Every edit recomputes the artifact checksum, and that checksum is recorded on
  the spans of each turn that used it — a trace always identifies the exact
  prompt text that produced it.

### Demo tokens (demo only)

The console authenticates with two static bearer tokens that exist for the
**demo only** and must never gate a real deployment. Set `DEMO_CUSTOMER_TOKEN`
and `DEMO_ADMIN_TOKEN` (≥ 16 chars each) in the Git-ignored `.env`; never commit
them. `APP_RUNTIME_MODE=demo` enables this authenticator; `production` rejects
it.

### Running & submitting

- `docker compose up --build` starts postgres + app + worker. The app serves the
  console at `/console/` and reports `/health/ready` only after every model role
  passes its **readiness check**.
- Drive the queue directly (same path the console uses); scope is bound from the
  bearer token, so the body carries no `customer_id`:

  ```
  POST /api/v1/submissions
  { "channel": "console", "external_message_id": "...", "session_id": "...",
    "text": "...", "idempotency_key": "..." }
  ```

### Failure & retry model

- Every failure maps through one chain:
  `readiness check -> model role -> probe stage -> trace node -> component ->
  operation -> safe error code -> automatic/manual retry disposition`.
- Failures surface as a **safe error code**, never raw internals — e.g. a stalled
  worker lease finishes the trace with `WORKER_LEASE_EXPIRED` and reserves a
  retry trace; a tool timeout surfaces as `TOOL_TIMEOUT` on component
  `order_api`, operation `order.lookup`.
- Inspect a failed handoff outbox for one authorized tenant only:
  `select tenant_id,id,status,attempts,last_error_code,last_http_status,next_attempt_at from notification.outbox where tenant_id = '<tenant-id>' and status='failed' order by created_at desc;`

### Extending: the future LINE adapter boundary

The submission API is channel-neutral. A future **LINE adapter** (or any
channel) plugs in by translating inbound webhooks into the same
`POST /api/v1/submissions` shape and mapping replies back — no pipeline changes.

---

## License / status

Bootstrap demo runtime. Use it to learn, demo, and build on — validate and
harden before trusting it with anything real.
