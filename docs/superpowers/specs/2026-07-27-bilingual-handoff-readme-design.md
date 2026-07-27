# Bilingual Handoff README

## Goal

Keep the existing English project overview while adding a Traditional Chinese
README that lets a new maintainer run the demo, verify it, understand its main
boundaries, and find the right files without prior project context.

## Files and Language Navigation

- Keep `README.md` as the English entry point.
- Add `README.zh-TW.md` as the Traditional Chinese entry point.
- Add a visible English / 繁體中文 language switch near the top of both files.
- Keep commands, paths, environment variable names, HTTP routes, and code
  identifiers unchanged across languages.

## Information Architecture

Use a handoff-first order in the Traditional Chinese README:

1. Project purpose and current status.
2. Prerequisites and the external model-server dependency.
3. Five-minute Docker Compose startup.
4. Direct host startup for developers.
5. Required and optional environment variables.
6. Demo login, sample prompts, and health verification.
7. System flow and repository map.
8. Behavior-tuning entry points.
9. Test commands.
10. Safety boundaries, known limitations, and handoff checklist.
11. Links to `DEVELOPING.md` and `TUNING.md`.

The English README may retain its existing explanatory structure, but its
startup, environment, verification, and handoff information must match the
Traditional Chinese version.

## Startup Paths

Document two supported paths in both languages.

### Docker Compose

- Treat Docker Compose as the default demo path.
- Show the helper command `./run.sh` and the equivalent explicit
  `docker compose` commands.
- Explain that the first helper-script run creates `.env` when it is missing.
- Include commands for start, logs, stop, reset, configuration restart, and
  readiness verification.
- State that the console is served on port `8080`.

### Direct Host Startup

- State the PostgreSQL and external OpenAI-compatible model-server
  prerequisites.
- Show dependency sync, migration, API, and worker commands.
- Make it clear that the API and worker are separate long-running processes.
- State the host API port and health endpoints.
- Explain that host startup needs a host-reachable `DATABASE_URL` and model
  endpoint, while Compose supplies its own container-network database URL.

## Configuration Ownership

Explain configuration by ownership rather than placing all settings in the
Dockerfile:

- `Dockerfile`: image-level invariants and process packaging.
- `compose.yaml`: service topology and safe demo defaults.
- `.env`: secrets, machine-specific endpoints, and local overrides.
- `config/*.yaml`: models, prompts, personas, knowledge, and tool behavior.
- README files: required setup, examples, and verification procedures.

Do not recommend baking secrets or machine-specific endpoints into an image.
Clearly distinguish required values from values that already have Compose
defaults.

## Handoff Content

The Traditional Chinese README must help the next maintainer answer:

- What must be running before startup?
- Which values must be changed locally?
- How do I know the API, worker, database, and models are ready?
- Where do I change model selection, prompts, personas, tools, or knowledge?
- Which tests run without external services?
- What is intentionally demo-only or not implemented?
- Which documents and source modules should I read first?

The handoff checklist must call out uncommitted secrets, local-only model
configuration, data-reset behavior, external service dependencies, and the
difference between demo authentication and production authentication.

## Scope

- Do not translate `DEVELOPING.md` or `TUNING.md` in this change.
- Do not change application behavior, container behavior, configuration
  semantics, or dependencies.
- Do not expose values from the local `.env`.
- Do not claim production readiness.

## Verification

- Render-check Markdown structure by reviewing headings, links, code fences,
  tables, and relative paths.
- Compare startup and environment-variable claims against `run.sh`,
  `compose.yaml`, `.env.example`, `DEVELOPING.md`, and `pyproject.toml`.
- Run `docker compose config` to validate the documented Compose setup.
- Run documentation-oriented repository checks such as `git diff --check`.
- Confirm both README files link to each other and contain both startup paths.
