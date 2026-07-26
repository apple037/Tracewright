# Tuning guide

Four files control almost everything. You do not need to read any Python.

| I want to change… | Edit this | Takes effect |
|---|---|---|
| How the assistant **sounds** | `config/personas/*.yaml` → `style_prompt` | restart (`make restart`) |
| What each step **decides** | `config/prompts/*.yaml` → `system_prompt` | restart |
| **Which model** does what | `config/models.yaml` | restart |
| Server address, passwords, memory length | `.env` | restart |

Or click **Tune** in the console (admin token required) and edit the prompts and
voice live — those changes apply to the very next message, no restart.

---

## 1. How it sounds — `config/personas/`

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

## 2. What each step decides — `config/prompts/`

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

## 3. Which model — `config/models.yaml`

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
`.env`, so `models.yaml` is safe to commit.

## 4. Settings — `.env`

| Setting | What it does |
|---|---|
| `REMOTE_MODEL_BASE_URL` | Where your model server is |
| `DEMO_CUSTOMER_TOKEN` / `DEMO_ADMIN_TOKEN` | Console passwords. Admin also unlocks Tune and full reasoning |
| `HISTORY_TURNS` | How many earlier exchanges the assistant sees (default 8) |
| `ASSURANCE_MODE` | `bootstrap` = one fact-checker, `dual_judge` = two that must agree |

---

## Editing from the console

The **Tune** panel shows what is running right now — every step's instructions,
the voice, and which model each step uses — and lets you edit the instructions
and the voice.

Saving there does **not** rewrite the YAML files. It layers an override stored
in `config/overrides.json`, so your commented config stays intact and **Revert
to file** always works. The panel marks anything currently overridden as
*Edited here*.

To make a change permanent, put it in the YAML and revert the override.

## Checking it worked

Every edit changes the artifact's fingerprint, and that fingerprint is recorded
on each step of every turn that used it. Send a message, open the trace, and the
step shows the version it ran with — so a reply from before your edit is always
distinguishable from one after it.
