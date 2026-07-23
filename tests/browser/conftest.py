import socket
import threading
import time

import pytest
import uvicorn

from agent_flow.main import create_app


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="session")
def server():
    port = _free_port()
    config = uvicorn.Config(
        create_app(), host="127.0.0.1", port=port, log_level="warning"
    )
    instance = uvicorn.Server(config)
    thread = threading.Thread(target=instance.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not instance.started and time.time() < deadline:
        time.sleep(0.05)
    if not instance.started:
        raise RuntimeError("console test server did not start")
    yield f"http://127.0.0.1:{port}"
    instance.should_exit = True
    thread.join(timeout=5)


@pytest.fixture(scope="session")
def console_url(server):
    return f"{server}/console/"


@pytest.fixture(scope="session")
def _browser():
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as driver:
        browser = driver.chromium.launch()
        yield browser
        browser.close()


@pytest.fixture
def page(_browser):
    page = _browser.new_page()
    errors: list[str] = []
    page.on(
        "console",
        lambda message: errors.append(message.text)
        if message.type == "error"
        else None,
    )
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    yield page
    page.close()
    assert not errors, f"browser console errors: {errors}"
