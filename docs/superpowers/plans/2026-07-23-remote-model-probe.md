# Remote Model Probe Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Verify the configured internal Ollama-compatible structured and embedding models without changing configuration or exposing sensitive response data.

**Architecture:** Use the existing `Settings`, `ModelRegistry`, `ModelInventoryProbe`, and `EmbeddingModel` interfaces. Execute bounded read-only requests and emit only sanitized inventory/capability metadata.

**Tech Stack:** Python 3.12, uv, httpx, Agent Flow model adapters

## Global Constraints

- Do not modify `.env`, model YAML, or remote server state.
- Never print credentials, request headers, model reasoning, or raw responses.
- Use the configured internal endpoint and short bounded timeouts.

---

### Task 1: Probe remote model capabilities

**Files:**
- Read: `.env`
- Read: `config/models.bootstrap.example.yaml`

**Interfaces:**
- Consumes: `Settings`, `ModelRegistry`, `ModelInventoryProbe`, `EmbeddingModel`
- Produces: sanitized pass/fail evidence for remote structured and embedding roles

- [ ] **Step 1: Verify inventory connectivity**

Run a bounded probe for `dialogue_classifier` and report only resolved model,
digest, context metadata, and verified capability names.

- [ ] **Step 2: Verify embedding**

Probe role `embedding`, then request one embedding and report only vector count,
dimension, and whether every value is finite.

- [ ] **Step 3: Report mismatches without mutation**

If inventory or capability validation fails, report its exact stage and the
configured tag. Do not update `.env`, YAML, or the remote endpoint.
