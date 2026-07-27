# Decisions that changed after the design was approved

The design in `docs/superpowers/specs/` is dated 2026-07-20 and was approved as
written. Some of it is no longer what the code does. This file records only the
places where the two disagree, and why — so the next maintainer can trust the
spec everywhere else.

For choices that were never in the spec and were simply not built, see the
"Things deliberately not built" table in [DEVELOPING.md](../DEVELOPING.md).

---

## Turn submission became asynchronous, so HTTP 429 has nothing to reject

**Spec:** §5.1 — "Saturation returns HTTP 429 with `Retry-After`."

**As built:** `POST /api/v1/submissions` writes a job row and returns 202
immediately. The worker is the only consumer of model capacity, so there is no
synchronous request left to reject. Back-pressure lives in `CapacityGuard`
(`adapters/models.py`) and in the depth of the job queue.

**Why:** a turn takes 30–90 seconds on a local model. Holding an HTTP connection
open for that, and then rejecting it on saturation, is worse for the caller than
queueing it. The queue also gave manual retry and the live trace console a
natural shape.

**To reopen:** a synchronous turn endpoint brings the requirement back with it.
§5.1 is annotated in place.

## The knowledge corpus moved out of this repo

**Spec:** §9 — RAG collections in PostgreSQL/pgvector, with fixtures for the MVP.

**As built:** `config/knowledge.yaml` declares sources by type. The main corpus
is `type: http`, an external knowledge base answering two questions — what it
holds, and one document. `type: fixture` remains for per-customer documents and
anything a tuner edits from the console. `api/mock_kb.py` serves the demo corpus
over that interface so the demo still needs no external service.

**Why:** the deployment this is for already has a knowledge base. Owning a second
copy of the corpus would mean owning its ingestion, its freshness and its
correctness, none of which this service is the right place for.

**What did not change:** the classifier may still only name a `source_id` it saw
in the catalog, so the grounding invariant holds — the knowledge base decides
what exists, and the assistant cannot cite a document that was never advertised.

## Embeddings and pgvector retrieval are wired but off

**Spec:** §8 — exact cosine search over 1024-dimension embeddings.

**As built:** the schema, migrations and repository exist and are tested. The
`embedding` role is in `disabled_roles`, and retrieval is by `source_id` from the
catalog. Turning it on needs an embedding model of exactly 1024 dimensions and a
new answer to how the classifier avoids inventing a document.

## Prompts and knowledge are editable at runtime

**Not in the spec at all.** §13 describes an offline improvement lifecycle with a
promotion gate and human approval; nothing there allows editing a live prompt.

**As built:** the Tune panel writes overrides to `config/overrides.json`, applied
to the next turn, with the artifact checksum stamped on every span so a trace
still identifies exactly what produced it. The YAML is never rewritten.

**Why:** the audience is someone tuning wording without a Python environment, on
a demo runtime. The promotion gate in §13 is the right mechanism for a production
deployment and is not built; this is not a substitute for it, and the README says
so under Safety & limits.

## The demo admin is bound to the demo customer

**As built:** `DemoTokenAuthenticator` gives the admin token `trace:admin` but
the same `customer_id` as the customer token, and never grants `customer:act_as`.

**Consequence, worth knowing before you build on it:** every admin endpoint —
including memory inspection and reset — can only see the demo customer. The
`act_as` path in `auth.py` is implemented and tested; the demo simply does not
issue the scope. Real authentication is where that gets decided.

## The MVP gates in §15.2 have never been measured

Intent accuracy, citation precision, handoff recall, the emotion lock set,
latency baselines: none of these have a dataset, a harness or a number. They are
delivery-sequence phase 5, and phase 5 was not started.

**This matters more than it looks.** Nothing in this repo entitles anyone to
claim the assistant is accurate. What exists is a set of demo cases verified by
hand against a local 8B model, listed in the README.
