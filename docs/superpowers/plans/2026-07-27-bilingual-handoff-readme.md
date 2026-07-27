# Bilingual Handoff README Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Traditional Chinese handoff README and bring the English README's startup, configuration, verification, and handoff guidance into sync.

**Architecture:** Documentation remains split by language: `README.md` is the English entry point and `README.zh-TW.md` is the Traditional Chinese entry point. Both files document the same two startup paths and configuration ownership, while the Chinese file uses a handoff-first reading order.

**Tech Stack:** Markdown, Docker Compose, Bash helper script, Python 3.12, uv, FastAPI, PostgreSQL 16

## Global Constraints

- Preserve `README.md` as the English entry point.
- Add `README.zh-TW.md` as the Traditional Chinese entry point.
- Document Docker Compose as the default demo path and direct host startup as the developer path.
- Keep commands, paths, environment-variable names, routes, and code identifiers identical in both languages.
- Do not change application behavior, container behavior, dependencies, or configuration semantics.
- Do not expose local `.env` values or claim production readiness.
- Do not modify unrelated working-tree changes.

---

### Task 1: Synchronize the English handoff instructions

**Files:**
- Modify: `README.md`

**Interfaces:**
- Consumes: `run.sh`, `compose.yaml`, `.env.example`, `DEVELOPING.md`, `TUNING.md`
- Produces: the canonical English commands and operational claims mirrored by `README.zh-TW.md`

- [ ] **Step 1: Add the language switch**

Place this directly below the `# Tracewright` heading:

```markdown
[English](README.md) | [繁體中文](README.zh-TW.md)
```

- [ ] **Step 2: Expand the Docker Compose startup path**

Keep `./run.sh` as the shortest path, then document its explicit equivalent:

```bash
cp .env.example .env
# Edit .env: set the two demo tokens and the model-server address.
docker compose up --build -d
docker compose logs -f app worker
```

State that Compose serves the console at `http://localhost:8080/console/` and
that `curl http://localhost:8080/health/ready` verifies database and model-role
readiness. Include `./run.sh logs`, `./run.sh stop`, `./run.sh reset`, and
`make restart`, with the warning that reset deletes demo database data.

- [ ] **Step 3: Document configuration ownership**

Add a compact table with these exact ownership boundaries:

| Location | Responsibility |
|---|---|
| `Dockerfile` | Image-level Python/uv defaults and packaged process command |
| `compose.yaml` | Demo service topology, container networking, ports, and safe runtime defaults |
| `.env` | Secrets, machine-specific model endpoint, and local overrides |
| `config/*.yaml` | Model roles, prompts, personas, tools, and knowledge sources |

Explicitly state that secrets and machine-specific endpoints must not be baked
into the Dockerfile.

- [ ] **Step 4: Complete direct host startup**

State the prerequisites: Python 3.12, uv, PostgreSQL, and an
OpenAI-compatible model endpoint. Show:

```bash
cp .env.example .env
uv sync --frozen
uv run --frozen alembic upgrade head
uv run --frozen uvicorn --factory agent_flow.runtime:create_runtime_app --reload
uv run --frozen python -m agent_flow.worker --run
```

Explain that API and worker commands run in separate terminals, the host API
uses `http://localhost:8000`, and host mode requires a host-reachable
`DATABASE_URL` plus model endpoint. Retain:

```bash
uv run pytest tests/unit tests/contract tests/browser -q
```

- [ ] **Step 5: Add a handoff checklist**

Add a concise checklist covering:

- `.env` stays local and contains no committed secrets.
- The selected `MODEL_CONFIG_PATH` exists for the next maintainer.
- The external model server and expected model names are documented locally.
- `docker compose config` and `/health/ready` succeed.
- Demo reset behavior is understood before deleting volumes.
- Static demo tokens are not production authentication.
- `DEVELOPING.md` and `TUNING.md` are the next reading steps.

- [ ] **Step 6: Review English Markdown**

Run:

```bash
rg -n "README.zh-TW|docker compose up|uvicorn|agent_flow.worker|health/ready|Handoff checklist" README.md
```

Expected: one language link plus matches for both startup paths, readiness, and
the handoff section.

---

### Task 2: Create the Traditional Chinese handoff README

**Files:**
- Create: `README.zh-TW.md`

**Interfaces:**
- Consumes: the commands and factual claims verified in Task 1
- Produces: a complete Traditional Chinese entry point for demo operators and future maintainers

- [ ] **Step 1: Create the header and status warning**

Start with:

```markdown
# Tracewright

[English](README.md) | [繁體中文](README.zh-TW.md)

**一套會安全回答、並完整留下判斷軌跡的客服 AI。**
```

Immediately state that this is a demo/bootstrap rather than a hardened
production system.

- [ ] **Step 2: Use the handoff-first section order**

Create these top-level sections in this order:

```markdown
## 專案用途
## 接手者先看這裡
## 五分鐘啟動：Docker Compose
## 不使用 Docker：直接在本機啟動
## 環境變數與設定責任
## Demo 操作與驗證
## 系統如何運作
## 專案目錄與修改入口
## 測試
## 安全邊界與已知限制
## 交接清單
## 授權與狀態
```

- [ ] **Step 3: Mirror both verified startup paths**

Use the same commands, URLs, ports, prerequisites, and reset warning as
`README.md`. Explain in Traditional Chinese that Compose is the default demo
path and that direct startup requires separate API and worker terminals.

- [ ] **Step 4: Explain required and optional configuration**

Describe the required local values from `.env.example` without copying any
value from `.env`: model-server endpoint plus two 16-character-or-longer demo
tokens. Explain that Compose owns the container database URL and that direct
host startup needs a host-reachable `DATABASE_URL`. Include the four-location
ownership table from Task 1 in Traditional Chinese.

- [ ] **Step 5: Add demo and maintenance guidance**

Include sample requests for chit-chat, order lookup, conversation memory,
knowledge retrieval, customer-scoped knowledge, expired knowledge, and safe
handoff. Link to `DEVELOPING.md` for code architecture/testing and `TUNING.md`
for prompts, personas, models, tools, and knowledge.

- [ ] **Step 6: Add the handoff checklist and limitations**

Cover external model dependency, demo-only authentication, reduced assurance,
live console overrides, stored admin reasoning, ignored local files, database
reset behavior, and intentionally unbuilt production authentication/channel
adapter boundaries.

- [ ] **Step 7: Review Chinese Markdown**

Run:

```bash
rg -n "README.md|docker compose up|uvicorn|agent_flow.worker|health/ready|交接清單" README.zh-TW.md
```

Expected: one English link plus matches for both startup paths, readiness, and
the handoff section.

---

### Task 3: Validate the bilingual documentation

**Files:**
- Verify: `README.md`
- Verify: `README.zh-TW.md`

**Interfaces:**
- Consumes: both completed README files and the current Compose configuration
- Produces: evidence that the documented commands and Markdown are internally consistent

- [ ] **Step 1: Validate Docker Compose configuration**

Run:

```bash
docker compose config --quiet
```

Expected: exit code 0.

- [ ] **Step 2: Check paired documentation claims**

Run:

```bash
rg -n "localhost:8080|localhost:8000|DEMO_CUSTOMER_TOKEN|DEMO_ADMIN_TOKEN|MODEL_CONFIG_PATH|DATABASE_URL" README.md README.zh-TW.md
```

Expected: both files mention both ports, the required demo tokens, the model
configuration path, and the host/container database distinction.

- [ ] **Step 3: Check links and paths**

Confirm every relative Markdown link points to an existing file and every
documented repository path exists or is explicitly described as local-only.
Pay particular attention to `README.zh-TW.md`, `DEVELOPING.md`, `TUNING.md`,
`LICENSE`, `.env.example`, `config/models.yaml`, and
`config/overrides.json`.

- [ ] **Step 4: Check formatting**

Run:

```bash
git diff --check -- README.md README.zh-TW.md
```

Expected: no trailing whitespace or malformed patch lines.

- [ ] **Step 5: Review the final diff without unrelated changes**

Run:

```bash
git diff -- README.md README.zh-TW.md
git status --short
```

Expected: README changes satisfy the design; unrelated existing files remain
untouched and are not staged.
