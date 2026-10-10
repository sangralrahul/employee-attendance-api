"""
Integration fixtures. Needs a reachable MongoDB (MONGO_URI); uses its own database "<MONGO_DB>_test"
(default attendance_db_test) and wipes it before every test. The real app runs under uvicorn in a
background thread, so concurrency tests exercise the real thread pool.
"""
import os
import socket
import threading
import time

import httpx
import pytest
import uvicorn
from dotenv import load_dotenv

load_dotenv(override=False)
os.environ["MONGO_DB"] = os.environ.get("MONGO_DB_TEST") or f"{os.environ.get('MONGO_DB', 'attendance_db')}_test"

from app import main as m  # noqa: E402  (must come after the env override)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def base_url():
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(m.app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            if httpx.get(f"{url}/health", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    else:
        pytest.fail("app did not become healthy (is MongoDB running and MONGO_URI set?)")
    yield url
    server.should_exit = True


@pytest.fixture
def api(base_url):
    m.employees.delete_many({})
    m.attendance.delete_many({})
    with httpx.Client(base_url=base_url, timeout=30) as client:
        yield client


@pytest.fixture
def db():
    return m.db
