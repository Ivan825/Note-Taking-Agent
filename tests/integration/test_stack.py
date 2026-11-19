"""Integration tests against the running compose stack (CI: MAX_ATTEMPTS=2, BACKOFF_SECONDS=2,
ENABLE_DEBUG_ENDPOINTS=true). Run with: INTEGRATION=1 pytest -q tests/integration

No real external API is called: the `youtube_429` fixture makes every YouTube call behave as an
HTTP 429 (chaos hook, raised before any network I/O), which drives jobs through the real
retry -> delay queue -> DLQ path.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.skipif(os.getenv("INTEGRATION") != "1", reason="needs the compose stack (INTEGRATION=1)")

BASE = os.getenv("GATEWAY", "http://localhost:8000")
ROOT = Path(__file__).resolve().parents[2]
VIDEO = "https://www.youtube.com/watch?v=xxxxxxxxxx0"
TERMINAL = {"COMPLETED", "COMPLETED_WITH_WARNINGS", "FAILED", "DEAD_LETTERED"}


@pytest.fixture
def api():
    with httpx.Client(base_url=BASE, timeout=10) as c:
        yield c


@pytest.fixture
def youtube_429(api):
    assert api.post("/debug/chaos", json={"youtube_429": 50}).status_code == 200
    yield
    api.delete("/debug/chaos")


def submit(api) -> str:
    r = api.post("/jobs", json={"youtube_url": VIDEO, "notion_token": "ci-dummy-token"})
    assert r.status_code == 202, r.text
    return r.json()["job_id"]


def wait_terminal(api, job_id: str, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = api.get(f"/jobs/{job_id}").json()
        if job["status"] in TERMINAL:
            return job
        time.sleep(0.5)
    pytest.fail(f"job {job_id} not terminal after {timeout}s: {job}")


def test_healthz_all_components_ok(api):
    r = api.get("/healthz")
    assert r.status_code == 200
    assert all(c["status"] == "ok" for c in r.json()["components"].values())


def test_unavailable_video_dead_letters_within_30s(api, youtube_429):
    job = wait_terminal(api, submit(api), timeout=30)
    assert job["status"] == "DEAD_LETTERED"
    assert job["error"]["attempts"] == 2


def test_results_409_while_running(api, youtube_429):
    job_id = submit(api)
    r = api.get(f"/jobs/{job_id}/results")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "JOB_NOT_FINISHED"
    wait_terminal(api, job_id, timeout=30)


def test_killed_worker_does_not_lose_queued_job(api, youtube_429):
    workers = subprocess.run(["docker", "compose", "ps", "-q", "worker"], cwd=ROOT,
                             capture_output=True, text=True, check=True).stdout.split()
    assert len(workers) >= 2, "need at least 2 workers"
    job_id = submit(api)
    subprocess.run(["docker", "kill", workers[0]], check=True, capture_output=True)
    try:
        assert wait_terminal(api, job_id, timeout=60)["status"] in TERMINAL
    finally:
        subprocess.run(["docker", "start", workers[0]], capture_output=True)
