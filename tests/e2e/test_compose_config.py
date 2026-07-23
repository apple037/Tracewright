from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import yaml


ROOT = Path(__file__).parents[2]


def _compose_config() -> dict:
    return yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))


def test_compose_declares_bounded_runtime_services_and_dependencies():
    compose = _compose_config()
    services = compose["services"]

    assert set(services) == {"postgres", "migrate", "app", "worker", "demo-seed"}
    assert services["postgres"]["image"] == "pgvector/pgvector:0.8.5-pg16-bookworm"
    assert services["postgres"]["healthcheck"]["test"] == [
        "CMD-SHELL",
        "pg_isready -U agent -d agent",
    ]
    assert services["migrate"]["depends_on"]["postgres"]["condition"] == "service_healthy"
    for name in ("app", "worker", "demo-seed"):
        assert (
            services[name]["depends_on"]["migrate"]["condition"]
            == "service_completed_successfully"
        )
        assert services[name]["networks"] == ["agent-net"]
    assert services["demo-seed"]["profiles"] == ["demo"]
    assert "/health/live" in " ".join(services["app"]["healthcheck"]["test"])
    assert "/api/v1/health/live" not in " ".join(
        services["app"]["healthcheck"]["test"]
    )
    assert compose["networks"]["agent-net"]["driver"] == "bridge"
    assert "postgres-data" in compose["volumes"]


def test_compose_uses_host_gateway_and_frozen_non_dev_uv_runtime():
    services = _compose_config()["services"]
    for name in ("app", "worker"):
        assert (
            services[name]["environment"]["LOCAL_VLLM_BASE_URL"]
            == "http://host.docker.internal:8000/v1"
        )
        assert "host.docker.internal:host-gateway" in services[name]["extra_hosts"]
        command = services[name]["command"]
        assert command[:4] == ["uv", "run", "--frozen", "--no-dev"]
        assert "--no-sync" in command
        assert "env_file" not in services[name]
        assert "API_KEY" not in services[name]["environment"]

    assert services["worker"]["command"][-3:] == [
        "-m",
        "agent_flow.worker",
        "--run",
    ]


def test_container_policy_is_pinned_non_root_and_excludes_local_state():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "ghcr.io/astral-sh/uv:0.9.30-python3.12-bookworm-slim" in dockerfile
    assert "\nUSER agent\n" in dockerfile
    assert "UV_FROZEN=1" in dockerfile
    assert "UV_NO_DEV=1" in dockerfile
    assert "UV_NO_SYNC=1" in dockerfile

    ignored = {
        line.strip()
        for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip()
    }
    assert {
        ".git",
        ".env",
        ".venv",
        ".pytest_cache",
        ".superpowers",
        ".agents",
        ".claude",
        "__pycache__",
        "*.pyc",
    } <= ignored


def test_demo_fixture_loader_is_tenant_scoped_and_deterministic():
    from agent_flow.seed_demo import load_demo_fixtures

    first = load_demo_fixtures(ROOT / "tests" / "fixtures", tenant_id="t1")
    second = load_demo_fixtures(ROOT / "tests" / "fixtures", tenant_id="t1")

    assert first == second
    assert first.rag_documents
    assert first.tool_fixtures
    assert {item.tenant_id for item in first.rag_documents} == {"t1"}
    assert {item.tenant_id for item in first.tool_fixtures} == {"t1"}
    assert all(
        item.source_id.startswith("agent-flow-demo:")
        for item in first.rag_documents
    )
    assert all(len(item.embedding) == 1024 for item in first.rag_documents)


def test_demo_seeder_upserts_database_rows_idempotently():
    from agent_flow.seed_demo import DemoSeeder

    class Cursor:
        rowcount = 1

        def __init__(self, row=None):
            self.row = row

        async def fetchone(self):
            return self.row

    class Transaction:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

    class Connection:
        def __init__(self):
            self.calls = []

        def transaction(self):
            return Transaction()

        async def execute(self, statement, parameters):
            self.calls.append((statement, parameters))
            row = {"id": parameters[0]} if "RETURNING id" in statement else None
            return Cursor(row)

    class ConnectionContext:
        def __init__(self, connection):
            self.connection = connection

        async def __aenter__(self):
            return self.connection

        async def __aexit__(self, *_):
            return None

    class Pool:
        def __init__(self):
            self.connection_value = Connection()

        def connection(self):
            return ConnectionContext(self.connection_value)

    pool = Pool()
    result = asyncio.run(
        DemoSeeder(
            pool,
            fixture_root=ROOT / "tests" / "fixtures",
            tenant_id="t1",
        ).seed()
    )

    statements = "\n".join(call[0] for call in pool.connection_value.calls)
    assert "INSERT INTO rag.documents" in statements
    assert "INSERT INTO rag.chunks" in statements
    assert "ON CONFLICT" in statements
    assert "WHERE rag.documents.id = EXCLUDED.id" in statements
    assert "WHERE rag.chunks.id = EXCLUDED.id" in statements
    document_inserts = [
        parameters
        for statement, parameters in pool.connection_value.calls
        if "INSERT INTO rag.documents" in statement
    ]
    assert result.rag_documents_seeded == 1
    assert len(document_inserts) == 1
    assert "mock-tool:" not in repr(document_inserts)
    assert result.tool_fixtures_loaded > 0


def test_readme_remote_ollama_and_outbox_checks_are_endpoint_and_tenant_scoped():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "$remoteBase = $env:REMOTE_MODEL_BASE_URL.TrimEnd('/')" in readme
    assert 'Invoke-RestMethod \"$remoteBase/api/tags\"' in readme
    assert 'Invoke-RestMethod \"$remoteBase/api/show\"' in readme
    assert "tenant_id = '<tenant-id>'" in readme


def test_worker_module_exposes_explicit_runtime_cli():
    from agent_flow.worker import build_argument_parser

    assert build_argument_parser().parse_args(["--run"]).run is True


def test_worker_cli_configures_json_logging_and_runs(monkeypatch):
    import logging

    from agent_flow import worker

    configured = []
    ran = []

    async def fake_run_cli():
        ran.append(True)

    monkeypatch.setattr(worker, "_run_cli", fake_run_cli)
    monkeypatch.setattr(
        worker,
        "configure_json_stdout",
        lambda logger: configured.append(logger),
    )

    worker.main(["--run"])

    assert configured == [logging.getLogger()]
    assert ran == [True]


def test_worker_runtime_closes_owned_resources_when_stopped():
    from agent_flow.worker import run_worker_runtime

    class Pool:
        opened = False
        closed = False

        async def open(self):
            self.opened = True

        async def close(self):
            self.closed = True

    class Webhook:
        closed = False

        async def close(self):
            self.closed = True

    pool = Pool()
    webhook = Webhook()
    stop = asyncio.Event()
    stop.set()
    settings = SimpleNamespace(
        webhook_url="http://example.invalid",
        webhook_secret="not-used",
    )

    asyncio.run(
        run_worker_runtime(
            settings=settings,
            stop=stop,
            pool=pool,
            webhook=webhook,
        )
    )

    assert pool.opened is True
    assert pool.closed is True
    assert webhook.closed is True
