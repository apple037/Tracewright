# Tracewright

[English](README.md) | [繁體中文](README.zh-TW.md)

**A customer-service AI that answers safely — and shows its work.**

Tracewright takes a customer message, runs it through a chain of small AI steps
(understand → check risk → look up facts → write reply → fact-check the reply),
and lets an operator **watch every step happen live** in a web console. If the AI
isn't sure, or a message looks risky, it hands off to a human instead of
guessing.

The headline feature is the word in the name: **traces**. Every message becomes a
fully recorded, click-through trace so you can see exactly *why* the AI said what
it said — normally the scariest black box in an AI product.

> ⚠️ **A demo, not a production system.** Small open models, two static demo
> tokens. Good for learning, demos, and building on — not for real customers
> unattended. See [Safety & limits](#safety--limits).

**Where to go next:** [TUNING.md](TUNING.md) to change how it behaves (no
Python). [DEVELOPING.md](DEVELOPING.md) to take the code over.

---

## What it does

A customer types *"Where's my refund?"*. Behind the scenes:

| Step | Node | What it decides |
|---|---|---|
| 1. Understand | `dialogue_classifier` | What does the customer want? What's their mood? Just chit-chat? |
| 2. Safety check | `risk_precheck` | Dangerous or sensitive? If so, **stop and hand to a human**. |
| 3. Plan facts | `evidence_planner` | Does anything need looking up (order status, policy)? |
| 4. Gather facts | `evidence_collector` | Fetch them from the knowledge base and tools. |
| 5. Verify facts | `evidence_validator` | Keep only what was actually asked for. |
| 6. Pick a style | `strategy_selector` | Brief, business-first, supportive? |
| 7. Write reply | `response_generator` | Draft the answer, citing its sources. |
| 8. Fact-check | `response_validator` | A second AI checks the draft is grounded. If not → repair or hand off. |

Every step is recorded as a **trace** with timing, decisions, and (for admins)
the model's raw reasoning. That is what the console shows.

It also **remembers the conversation**: steps 1 and 7 see the recent exchanges of
the same session, so "how long will that take?" resolves against what was already
discussed.

**The one rule everything else serves:**

> The assistant may only state facts it actually retrieved this turn.

It cannot invent a refund policy. Steps 5 and 8 exist to enforce that, and a
handoff is the designed outcome when they can't — not a failure.

---

## Quick start

Before you start, have **Docker Desktop** running, and an OpenAI-compatible model
server (Ollama, vLLM, …) already serving the model named in the file
`MODEL_CONFIG_PATH` points at — the committed demo expects `qwen3.6:27b`.

**1. Run the script.** It stops on the first run, after copying `.env.example`
to `.env`:

```bash
./run.sh
```

**2. Fill in three values in `.env`:**

```dotenv
REMOTE_MODEL_BASE_URL=http://host.docker.internal:11434/v1
DEMO_CUSTOMER_TOKEN=any-random-string-at-least-16-characters
DEMO_ADMIN_TOKEN=another-random-string-at-least-16-characters
```

If the model server runs on another machine, use its LAN address instead of
`host.docker.internal`; add `REMOTE_MODEL_API_KEY` if it needs one. **Never
commit `.env`.**

**3. Run `./run.sh` again.** It builds the images, starts PostgreSQL + API +
worker, migrates, seeds demo data, and prints the URL.

**4. Open http://localhost:8080/console/ and paste the *admin* token.** Send
`good morning`. You should get a reply and a trace that fills in step by step.
If nothing happens, check the dependencies first:

```bash
curl http://localhost:8080/health/ready   # every dependency, including model roles
./run.sh logs                             # follow API and worker logs
```

Day to day:

```bash
./run.sh stop     # stop the services
./run.sh reset    # stop AND permanently delete the demo database
make restart      # reload edits under config/*.yaml
```

No Bash? Do steps 1 and 3 by hand — everything else is the same:

```powershell
Copy-Item .env.example .env
# Edit .env, then:
docker compose up --build -d
docker compose --profile demo run --rm demo-seed
```

`docker compose down`, `down -v` and `restart app worker` replace stop, reset and
`make restart`.

> ⏳ Replies take real time — a 27B model on one GPU is roughly 30–90 seconds per
> message, because a turn makes several model calls. Watch the steps fill in.

Running without Docker: see [DEVELOPING.md](DEVELOPING.md).

---

## Settings

### Set these three, or it will not start

`./run.sh` refuses to start without them, so a mistake here is loud rather than
mysterious.

| Setting | What to put | Rules |
|---|---|---|
| `REMOTE_MODEL_BASE_URL` | Your model server, including `/v1` | `host.docker.internal` reaches the host from a container; use the LAN address if the server is on another machine. `localhost` inside a container means the container. |
| `DEMO_CUSTOMER_TOKEN` | Any random string | 16 characters or more |
| `DEMO_ADMIN_TOKEN` | A **different** random string | 16 characters or more. If it matches the customer token, that one wins and you can never reach the Tune panel. |

One more thing has to be true, and nothing checks it for you: **the model named
in your models file must already exist on that server.** The committed
`config/models.yaml` asks for `qwen3.6:27b`. `/health/ready` is what tells you —
it fails if a model role cannot be reached.

### Set these only if your situation calls for it

| Setting | When |
|---|---|
| `REMOTE_MODEL_API_KEY` | Your model server requires a key. Empty is fine otherwise. |
| `MODEL_CONFIG_PATH` | You keep more than one models file. Editing the one that is *not* live changes nothing and reports no error — Tune → Models shows the live path. |
| `DATABASE_URL` | You run without Docker. Compose supplies its own and ignores this. |
| `KNOWLEDGE_BASE_URL` | You point at a real knowledge base, or run without Docker (then it is port 8000, not 8080). |
| `LOCAL_VLLM_BASE_URL`, `LOCAL_VLLM_API_KEY` | A profile in your models file points at a local vLLM server. |
| `MODEL_TIMEOUT_SECONDS` | Your model is slow. Default 90 suits a hosted model; a local one generating at a few tokens a second needs far more, and the failure reads as `UNEXPECTED_ERROR`, not as a timeout. |

### Leave these alone unless you have a reason

| Setting | Default | Effect |
|---|---|---|
| `HISTORY_TURNS` | `8` | Earlier exchanges the assistant is shown (0–40). Higher remembers more and costs more tokens; too high and small models lose the plot. |
| `ASSURANCE_MODE` | `bootstrap` | `bootstrap` = one fact-checker, `dual_judge` = two that must agree. |
| `APP_RUNTIME_MODE` | `demo` | `production` rejects the demo tokens, which leaves no way in until real authentication exists. Compose pins this to `demo`. |
| `WEBHOOK_URL`, `WEBHOOK_SECRET` | a stub | Where a handed-off conversation is posted. Nothing is listening in the demo. |
| `DEMO_TENANT_ID`, `DEMO_CUSTOMER_ID` | `t1`, `c1` | Who the demo tokens act as. `config/demo/account.json` binds a document to `c1`. |
| `WORKER_OWNER` | `agent-flow-bootstrap` | Names the worker in the job queue. |

**Behaviour is not in `.env`.** Which model does what, what the assistant knows,
how it sounds, what each step decides — all of that is `config/*.yaml`, and
[TUNING.md](TUNING.md) covers it.

---

## Using the console

```
┌───────────────┬──────────────────────┬─────────────────────┐
│  Chats        │   Conversation       │  What happened      │
│  (one per     │   (type here —       │  this turn          │
│   chat id;    │    the main event)   │  (steps fill in     │
│   click to    │                      │   live; click one   │
│   switch)     │                      │   for its decisions)│
│───────────────│                      │                     │
│  Turns        │                      │                     │
│  (of the      │                      │                     │
│   selected    │                      │                     │
│   chat)       │                      │                     │
└───────────────┴──────────────────────┴─────────────────────┘
```

Type a message and press Send. Its trace auto-selects and fills in live; click
any step to see what it decided. Things worth trying:

| Say | What it shows |
|---|---|
| `good morning` | Answers, retrieves **nothing**, cites nothing |
| `where is my order order-1?` | Looks the order up and cites the tool result (in transit) |
| `is it still on the way?` | Reuses the order from the previous turn — **memory** |
| `where is my order order-2?` | A different order, a different status (delivered) |
| `how about order-3?` | Still preparing — the order id came from this message alone |
| `how long do refunds take?` | Answers from the knowledge base with a citation |
| `how long does shipping take?` | A different document answers — not the refund policy |
| `八月團購有哪些咖啡可以選？` | Reads the group-buy list, quotes items and prices |
| `這些品項的特色有啥？` | A **different document** answers — flavour notes, not the prices again |
| `七月零食團購的取貨時間是幾點到幾點？` | The document has no pickup hours, so it **says so** |
| `會員等級有哪些？點數怎麼算？` | Tiers, thresholds and the points rule, from one document |
| `我有什麼折扣碼可以用嗎？` | A document bound to **this customer only** |
| `夏季特賣還有嗎？` | The promotion **expired in 2025**, so it is invisible — no stale quote |
| `where is my refund?` | **Hands off to a human** — no verified source, so it refuses to guess |

Also worth knowing:

- **Chats** (top left) is one row per chat id — the same id a LINE webhook
  supplies. Click one to read its history and keep talking in it.
- Refresh the page and the conversation is still there. **New conversation**
  starts a clean one.
- **EN / 中文** switches language, **◐** light/dark, **Retry trace** re-runs a
  finished turn.
- **http://localhost:8080/docs** is the API with an Authorize button — paste the
  same admin token and call any endpoint.

**Admin** sees everything: real input/output, the model's raw chain-of-thought,
and the Tune panel. **Customer** sees only the chat.

---

## Changing how it behaves

No Python. **[TUNING.md](TUNING.md)** is the guide, written for whoever shapes
the assistant rather than for a programmer.

| I want to change… | Edit |
|---|---|
| What the AI **knows** | Console → **Tune** → Knowledge, or the knowledge base itself |
| How it **sounds** | Console → **Tune** → Voice, or `config/personas/*.yaml` |
| What each step **decides** | Console → **Tune** → Instructions, or `config/prompts/*.yaml` |
| Where knowledge **comes from** | `config/knowledge.yaml` |
| Where lookups **go** (ERP, CRM) | `config/tools.yaml` |
| **Which model** does what | the file `MODEL_CONFIG_PATH` points at |
| Addresses, tokens, memory length | `.env` |

The rule of thumb: **behaviour lives in `config/`, secrets and machine addresses
live in `.env`, service topology lives in `compose.yaml`.** Nothing
machine-specific belongs in the `Dockerfile`.

Tune (admin token) shows every step's current instructions, the voice, the
documents the assistant may cite, and which model runs each step — editable live,
applied to the very next message. Edits land in `config/overrides.json`
(git-ignored) and never rewrite your YAML, so **Revert to file** always works.

---

## How it's built

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

**Two processes, one database.** `app` takes messages in, serves the console and
reads traces out — it never calls the AI. `worker` pulls queued jobs, runs the
pipeline, and writes every step as a trace. **No message queue, no Redis, no
Kafka** — the job queue is a PostgreSQL table.

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
│   └── turn.py        #   the conductor that runs steps 1→8 in order
├── adapters/          # talk to the outside: models, knowledge, tools
├── repositories/      # read/write the database
└── console/           # the web UI (HTML/CSS/JS, i18n EN + 中文)

config/                # editable settings — no rebuild, just `make restart`
├── models*.yaml       # which model powers each step (MODEL_CONFIG_PATH picks one)
├── knowledge.yaml     # every knowledge source the AI may retrieve from
├── tools.yaml         # where lookups go: demo fixture, or a real ERP/CRM
├── prompts/           # the instructions given to each AI step
├── personas/          # how the assistant sounds
└── demo/              # the demo corpus and canned tool answers
```

Built on **Python 3.12**, FastAPI, Pydantic 2, PostgreSQL 16 + pgvector,
psycopg 3 + Alembic, uv, Docker Compose, pytest + Playwright. The console is
plain HTML/CSS/JS — nothing to build.

---

## Safety & limits

- **Demo login only.** Two static tokens in `.env`. A real deployment needs
  proper authentication (`APP_RUNTIME_MODE=production` deliberately refuses the
  demo tokens).
- **"Reduced assurance."** Fact-checking is deterministic rules plus one AI
  judge, so the system labels its own confidence `reduced_assurance` and is
  **not** approved to run unattended.
- **Small models = variable quality.** Weak or off replies on open chit-chat are
  expected; a larger model improves it.
- **Console edits are live.** Tune changes behaviour for the next message with no
  restart and no code review. Admin tokens only.
- **Admin reasoning is stored.** Raw chain-of-thought is recorded in the trace
  and filtered out for everyone else. Re-check that before using real data.
- **External services are required.** If the model server is down, the API may
  still be alive but `/health/ready` and real replies fail.
- **Handoff is a boundary, not an integration.** The webhook receiver is a stub;
  a real ticketing system, alerting and retry operations are still to build.
- **No LINE adapter.** The submission API is channel-neutral and ready for one,
  but nobody has written it — see [DEVELOPING.md](DEVELOPING.md).
- **Reset is irreversible.** `./run.sh reset` and `docker compose down -v`
  permanently delete the demo database.
- **Never commit secrets.** `.env`, real tokens and API keys stay local.

---

## License

**[MIT](LICENSE).** Use it, fork it, build on it.

A bootstrap demo runtime — validate and harden before trusting it with anything
real.
