FROM ghcr.io/astral-sh/uv:0.9.30-python3.12-bookworm-slim

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_FROZEN=1 \
    UV_NO_DEV=1 \
    UV_NO_SYNC=1

RUN groupadd --system agent \
    && useradd --system --gid agent --home-dir /app agent

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY . .
RUN uv sync --frozen --no-dev \
    && chown -R agent:agent /app

USER agent
EXPOSE 8080

# The runtime factory, not agent_flow.main:app — main:app has no pipeline, no
# database and no auth wired in, so a bare `docker run` on it answers 503.
CMD ["uv", "run", "--frozen", "--no-dev", "--no-sync", "uvicorn", "--factory", "agent_flow.runtime:create_runtime_app", "--host", "0.0.0.0", "--port", "8080"]
