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

### Why "grounded" matters

The AI is only allowed to state facts it actually retrieved. It can't invent a
refund policy — if it has no evidence, it shouldn't cite one. Step 5 and step 8
exist to enforce that. (A real bug we fixed: a plain "good morning" was pulling
the refund policy into the answer — the pipeline now correctly retrieves nothing
for chit-chat.)

---

## Quick start (the 5-minute demo)

You need **Docker Desktop** installed. That's the easy path — it runs everything
(database, API, background worker) for you.

```bash
# 1. Copy the example settings file and give yourself demo login tokens
cp .env.example .env
```

Open `.env` in any text editor and add two demo tokens at the bottom (any random
16+ character strings — these are just demo passwords for the console):

```
DEMO_CUSTOMER_TOKEN=pick-any-random-string-16-chars-min
DEMO_ADMIN_TOKEN=pick-another-random-string-16-chars
```

> 🔒 `.env` is **private** and never uploaded to GitHub (it's git-ignored). Never
> put real passwords or API keys anywhere else.

You also need an AI model endpoint for the AI steps to call. Tracewright expects:
- a **local** model (vLLM) serving `Qwen/Qwen3-8B-AWQ` on port `8000`, and
- optionally a **remote** Ollama-style model for the classifier/judge.

Point at yours in `.env` (`LOCAL_VLLM_BASE_URL`, `REMOTE_MODEL_BASE_URL`).

```bash
# 2. Start everything
docker compose up --build

# 3. (optional) load the demo knowledge base
docker compose --profile demo run --rm demo-seed
```

When it's ready, open **http://localhost:8080/console/** and paste your
**admin** token.

---

## Using the console

The console is **one page, three columns** — no page-switching:

```
┌───────────────┬───────────────────────────┬──────────────────┐
│  Trace list   │       Trace workspace      │  Customer chat   │
│ (every turn,  │  Input & Output            │  (type here,     │
│  auto-refresh │  Model reasoning (admin)   │   press Send)    │
│  every 5s)    │  Reasoning summary         │                  │
│               │  Node flow (the 8 steps)   │                  │
└───────────────┴───────────────────────────┴──────────────────┘
```

1. **Type a message** in the right-hand chat and press Send (try `where is my
   order order-1?`).
2. Its trace **auto-selects** in the middle and **streams live** as each step runs.
3. Click any step card to expand its decisions; click a row on the left to switch
   traces.
4. **EN / 中文** toggles language. **Retry trace** re-runs a finished trace.

**Admin vs customer token:**
- **Admin** token sees everything: the real input/output, the model's raw
  chain-of-thought (collapsible, labeled per step), and full reasoning.
- **Customer** token sees only the chat — no internal reasoning.

> ⏳ The AI steps use real models, so a full reply can take ~1–2 minutes on the
> demo setup. Watch the node flow fill in while you wait.

---

## Tech stack

| Layer | Choice | Why |
|-------|--------|-----|
| Language | **Python 3.12** | The whole backend. |
| Web API | **FastAPI** | Serves the API + the console. |
| Data models | **Pydantic 2** | Strict, typed data at every step (no loose dicts). |
| Database | **PostgreSQL 16 + pgvector** | Stores traces, jobs, and the searchable knowledge base. |
| DB access | **psycopg 3** + **Alembic** | Queries + versioned schema migrations. |
| AI models | **vLLM** (local) + **Ollama** (remote), OpenAI-compatible | The actual language models; swappable via config. |
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
  (live traces)                                                (vLLM local / Ollama remote)
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
├── api/               # HTTP endpoints (submissions, traces, health)
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
├── models.bootstrap.example.yaml  # which model powers each step (swap here)
├── prompts/           # the instructions given to each AI step
└── demo/rag.json      # the demo knowledge base (facts the AI may cite)
```

**Want to change what the AI knows?** Edit `config/demo/rag.json`.
**Want to change which model runs a step?** Edit `config/models.bootstrap.example.yaml`.
**Want to change how a step behaves?** Edit its prompt in `config/prompts/`.
You rarely need to touch Python for those.

---

## Running without Docker (developers)

If you'd rather run the pieces by hand (needs Python 3.12 + [uv](https://docs.astral.sh/uv/)):

```bash
uv sync --frozen                              # install dependencies
uv run --frozen alembic upgrade head          # set up the database
uv run --frozen uvicorn agent_flow.main:app --reload   # start the API
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
- **Small models = variable quality.** The local 8B model can produce weak or
  off replies on open chit-chat. That's expected; a larger model improves it.
- **"Reduced assurance."** Fact-checking uses deterministic rules plus one AI
  judge, so the system labels its own confidence as `reduced_assurance` and is
  **not** approved to run unattended in production.
- **Admin reasoning is stored.** Admin tokens can view the models' raw
  chain-of-thought; it's recorded in the trace and filtered out for everyone
  else. Turn it off if that's not acceptable for your use.
- **Never commit secrets.** `.env`, real tokens, and API keys stay local. The
  repo is regularly checked to ensure none leak in.

---

## License / status

Bootstrap demo runtime. Use it to learn, demo, and build on — validate and
harden before trusting it with anything real.
