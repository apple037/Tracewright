# Remote Model Probe Design

## Goal

Verify the configured internal remote model endpoint without changing runtime
configuration or exposing credentials and model reasoning.

## Read-only checks

1. Load Agent Flow settings from the ignored `.env`.
2. Query the configured Ollama-compatible inventory endpoints.
3. Confirm whether the configured `qwen3.5:9b` and embedding model tag exist.
4. Run one bounded structured-output capability request with thinking disabled.
5. Run one bounded embedding request and report vector count and dimension only.

## Safety

- Never print API keys, Authorization headers, raw reasoning, or full model
  responses.
- Do not modify `.env`, model YAML, server state, or downloaded models.
- Report an exact tag mismatch without correcting it automatically.
- Use short timeouts and one request per required capability.

## Success criteria

- The endpoint is reachable and returns a valid inventory response.
- The structured model returns schema-valid content with thinking disabled.
- The embedding model returns one finite vector with exactly 1024 dimensions.
- Results identify the failing stage without revealing sensitive payloads.
