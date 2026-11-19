"""POST /jobs/{id}/retry and DELETE /jobs/{id} (Task 2)."""

import uuid

from notes_common import crypto
from notes_common.db import session_scope
from notes_common.models import Job
from worker import pipeline

URL = "https://www.youtube.com/watch?v=aircAruvnKk"


def submit(client, **extra):
    return client.post("/jobs", json={"youtube_url": URL, "notion_token": "secret_test", **extra}).json()["job_id"]


def set_status(job_id, status, **fields):
    with session_scope() as db:
        job = db.get(Job, uuid.UUID(job_id))
        job.status = status
        for k, v in fields.items():
            setattr(job, k, v)


def test_retry_failed_job_creates_new_job(client, publisher):
    old_id = submit(client, video_title="Neural nets", notion_parent_id="parent-1")
    set_status(old_id, "FAILED", error={"code": "NO_TRANSCRIPT", "message": "x"})
    before = client.get(f"/jobs/{old_id}").json()

    r = client.post(f"/jobs/{old_id}/retry", json={"notion_token": "fresh_token"})
    assert r.status_code == 202
    body = r.json()
    assert set(body) == {"job_id", "status", "video_id"}
    assert body["job_id"] != old_id and body["status"] == "QUEUED" and body["video_id"] == "aircAruvnKk"
    assert r.headers["location"] == f"/jobs/{body['job_id']}"

    msg = publisher.published[-1]
    assert msg["job_id"] == body["job_id"]
    assert crypto.decrypt(msg["notion_token"]) == "fresh_token"
    assert msg["notion_parent_id"] == "parent-1"
    assert client.get(f"/jobs/{body['job_id']}").json()["video_title"] == "Neural nets"
    assert client.get(f"/jobs/{old_id}").json() == before  # old job untouched


def test_retry_dead_lettered_allowed(client):
    old_id = submit(client)
    set_status(old_id, "DEAD_LETTERED")
    assert client.post(f"/jobs/{old_id}/retry", json={"notion_token": "t"}).status_code == 202


def test_retry_non_failed_is_409(client):
    for status in ("QUEUED", "SUMMARIZING", "COMPLETED"):
        job_id = submit(client)
        set_status(job_id, status)
        r = client.post(f"/jobs/{job_id}/retry", json={"notion_token": "t"})
        assert r.status_code == 409, status
        assert r.json()["error"]["code"] == "JOB_NOT_RETRYABLE"


def test_retry_unknown_is_404(client):
    assert client.post(f"/jobs/{uuid.uuid4()}/retry", json={"notion_token": "t"}).status_code == 404


def test_cancel_queued_job(client, publisher):
    job_id = submit(client)
    r = client.delete(f"/jobs/{job_id}")
    assert r.status_code == 200
    job = r.json()
    assert job["status"] == "FAILED"
    assert job["error"]["code"] == "CANCELLED"
    # the worker then drops the queued message because the job is terminal
    outcome = pipeline.run(publisher.published[-1], attempt=1, redelivered=False)
    assert outcome.kind == "ack" and outcome.reason == "already finished"


def test_cancel_started_job_is_409(client):
    job_id = submit(client)
    set_status(job_id, "TRANSCRIBING")
    r = client.delete(f"/jobs/{job_id}")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "JOB_ALREADY_STARTED"


def test_cancel_finished_job_is_409_already_finished(client):
    job_id = submit(client)
    assert client.delete(f"/jobs/{job_id}").status_code == 200
    r = client.delete(f"/jobs/{job_id}")  # cancelling twice
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "JOB_ALREADY_FINISHED"


def test_cancel_unknown_is_404(client):
    assert client.delete(f"/jobs/{uuid.uuid4()}").status_code == 404
