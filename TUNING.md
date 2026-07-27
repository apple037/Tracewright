# Tuning guide

For the person who shapes how the assistant behaves. **No Python.** Most of it
you can do from the console; the rest is editing a text file.

---

## Start here: what do you want to change?

| I want to change… | Where | Takes effect |
|---|---|---|
| What the assistant **knows** | Console → **Tune** → Knowledge | next message |
| How it **sounds** | Console → **Tune** → Voice, or `config/personas/*.yaml` | next message / restart |
| What each step **decides** | Console → **Tune** → Instructions, or `config/prompts/*.yaml` | next message / restart |
| Where it **looks things up** | `config/knowledge.yaml`, `config/tools.yaml` | restart |
| **Which model** does what | the models file — see §5, it is not always `models.yaml` | restart |
| Memory length, assurance | `.env` | restart |

Restart means `make restart`. It rereads `config/`; it does not rebuild.

---

## 1. What the assistant knows — Knowledge

The assistant may only state facts it actually retrieved. Everything it is
allowed to say comes from a **knowledge document**, and every answer cites the
document it came from. So "the assistant gave a wrong answer" is usually "the
document says the wrong thing", and you fix it here.

**From the console:** Tune → Knowledge. Each source lists its documents. Edit
one in place, delete it, or add one with the form at the bottom. It applies to
the next message — no restart.

**Read this before you edit.** A document here is treated as **true**. The
fact-checking step compares the reply against the documents that were
retrieved; it does not compare them against reality. Whatever you type will be
told to customers as fact, with a citation. That is what makes it powerful and
what makes it dangerous.

**A good document** answers one question completely, in the language your
customers use:

```
source_id: groupbuy:tea-2026-09
content:   九月茶葉團購清單：高山烏龍四兩 NT$520、蜜香紅茶四兩 NT$460。
           訂購截止 2026-09-05，9/12 於台北辦公室三樓取貨，取貨時間 10:00-18:00。
```

The `source_id` is a label. Group related ones with a prefix — `policy:`,
`groupbuy:`, `account:` — because it is what you will scan when the corpus
grows, and it is what appears in the citation.

**Splitting documents matters more than you would think.** If one document
holds a price list and a customer asks about flavour, the assistant has to
either say it does not know or hand you back the prices under the wrong
heading. The demo has `groupbuy:coffee-2026-08` and a separate
`groupbuy:coffee-2026-08:notes` for exactly this reason. **One question, one
document.**

**Two things you can only do in the file** (`config/demo/*.json`):

- `customer_id: c1` — only that customer can retrieve it. See
  `account:c1:coupon` in the demo.
- `valid_until: "2025-09-01T00:00:00Z"` — the document expires. After that date
  it is invisible: not retrieved, and not even offered to the classifier. Use it
  for promotions instead of deleting them.

### Adding a whole new source

`config/knowledge.yaml` lists them. A new one is three lines:

```yaml
sources:
  faq:
    type: fixture
    path: config/demo/faq.json
```

`enabled: false` parks a source without deleting it.

## 2. Looking things up about one customer — Tools

Knowledge is what everyone shares. A **tool** answers a question about one
customer and takes arguments: "where is order-3". `config/tools.yaml`:

```yaml
tools:
  order.lookup:
    type: fixture               # the demo's canned answers
    path: config/demo/tools.json
```

Swap the demo for a real ERP or CRM without touching any code — change `type`
to `http` and give it a URL. The commented block in `config/tools.yaml` is a
working example. Note `map:` in it: **only the fields you list there can reach a
customer**, so it is also how you keep internal fields out of a reply.

## 3. How it sounds — `config/personas/`

A persona is a voice. It changes wording only: it can never override a policy,
hide a fact that was looked up, or change a safety decision. The `guardrails`
block at the bottom of the file is what enforces that.

```yaml
applies_to:            # which kinds of conversation use this voice
  - emotional_support
  - casual

style_prompt: |
  Speak like a familiar friend, not a support agent reading a script.
  Be brief but concrete. Two or three sentences is usually enough.
```

**Add a second voice:** copy the file, give it a new `artifact_id` and a
different `applies_to`, drop it in the folder. It is picked up automatically. A
given conversation mode can be claimed by at most one persona — two personas
listing `casual` is an error at startup.

The conversation modes you can list are: `informational`, `transactional_read`,
`complaint`, `emotional_support`, `casual`, `boundary`, `unknown`.

A persona is written for one locale. A `zh-TW` voice is not applied to a
customer writing English.

## 4. What each step decides — `config/prompts/`

One file per step. `system_prompt` is plain English, sent to the model as its
system message.

| File | Controls |
|---|---|
| `dialogue_classifier.v1.yaml` | Working out what the customer wants, and whether anything needs looking up |
| `strategy_selector.v1.yaml` | Choosing the shape of the reply |
| `response_generator.v1.yaml` | Writing the reply, and the grounding rules |
| `response_judge.v1.yaml` | Fact-checking the reply before it is sent |

Bump `version` when you make a real change. Every trace records the version and
a fingerprint of the exact text that ran, so you can always tell which wording
produced which answer.

Everything below `system_prompt` in these files is machine-checked structure.
Leave it alone unless you are also changing code.

**The judge cuts both ways.** Tighten `response_judge` and it stops inventions,
but tighten it too far and it starts rejecting correct answers, which ends the
turn in a handoff. If handoffs suddenly rise after you edit it, that is the
cause. Both halves of the rule need to be in the prompt: what must fail, and an
example of what must still pass.

## 5. Which model — the models file

**Check which file is actually live before you edit anything.** There are
several `models*.yaml` in `config/`, and `MODEL_CONFIG_PATH` in `.env` decides
which one runs. Editing the wrong one changes nothing and gives you no error.

The console tells you: Tune → Models shows the path at the bottom of the table.

```yaml
profiles:
  local_qwen:
    model: qwen3.6:27b      # ← the model name on your server
    temperature: 0          # 0 = same answer every time
    structured_output: json_object

roles:
  dialogue_classifier: local_qwen
  response_generator: local_qwen   # ← point this at a bigger profile if you like
```

**Use a bigger model for the final reply only:** add a second profile, then
change the one line under `roles`.

`structured_output` matters: Ollama's `/v1` accepts OpenAI's `json_schema` field
and then ignores it, so Ollama profiles must use `json_object`. vLLM, TGI and
the OpenAI API support `json_schema`.

The server address is not in this file — it is `REMOTE_MODEL_BASE_URL` in
`.env`, so the models file is safe to commit.

## 6. Settings — `.env`

| Setting | What it does |
|---|---|
| `MODEL_CONFIG_PATH` | Which models file is live |
| `REMOTE_MODEL_BASE_URL` | Where your model server is |
| `DEMO_CUSTOMER_TOKEN` / `DEMO_ADMIN_TOKEN` | Console passwords. Admin also unlocks Tune and full reasoning |
| `HISTORY_TURNS` | How many earlier exchanges the assistant sees (default 8) |
| `ASSURANCE_MODE` | `bootstrap` = one fact-checker, `dual_judge` = two that must agree |

---

## Editing from the console

The **Tune** panel shows what is running right now — every step's instructions,
the voice, the knowledge, and which model each step uses.

Saving there does **not** rewrite the YAML files. It layers an override stored
in `config/overrides.json`, so your commented config stays intact and **Revert
to file** always works. The panel marks anything currently overridden as
*Edited here*.

To make a change permanent, put it in the YAML and revert the override.

Knowledge is the exception: it writes to the JSON file the source points at, so
a document you add there is a real edit to that file.

## Checking it worked

Every edit changes the artifact's fingerprint, and that fingerprint is recorded
on each step of every turn that used it. Send a message, open the trace, and the
step shows the version it ran with — so a reply from before your edit is always
distinguishable from one after it.

If a reply looks unchanged, check the fingerprint before you edit again. Same
fingerprint means your edit did not reach the pipeline; different fingerprint
means it did, and the wording is what needs work.

## When the answer is wrong, read the trace first

The console shows every step. The answer to "why did it say that" is almost
always visible in one of them:

| What you see in the trace | What it means | Where to fix it |
|---|---|---|
| Nothing was retrieved | The classifier did not think a lookup was needed, or found no matching source | `dialogue_classifier` prompt, or the document's wording — it has to be recognisable from the question |
| The wrong document was retrieved | Two documents look alike to the classifier | Split them, or make the `source_id` and first line more distinct |
| The right document, wrong answer | The generator misread it | `response_generator` prompt |
| Handed off to a human | The judge rejected the reply, or the risk step stopped it | The trace names the failed criterion |
| The reply states something no document says | This should not happen — grounding failed | Report it; that is a bug, not a tuning problem |
