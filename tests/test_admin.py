"""DLQ admin: auth, listing and replay (Task 1)."""

import base64
import uuid

from notes_common.config import get_settings
from notes_common.db import session_scope
from notes_common.models import Job

URL = "https://www.youtube.com/watch?v=aircAruvnKk"
AUTH = {"X-Admin-Token": "test-admin-token"}


def dead_lettered_job(client, publisher, in_dlq=True) -> str:
    job_id = client.post("/jobs", json={"youtube_url": URL, "notion_token": "secret_test"}).json()["job_id"]
    msg = publisher.published.pop()
    if in_dlq:
        publisher.dlq.append(msg)
    with session_scope() as db:
        job = db.get(Job, uuid.UUID(job_id))
        job.status = "DEAD_LETTERED"
        job.attempt = 5
        job.error = {"code": "VIDEO_UNAVAILABLE", "message": "This video is unavailable", "attempts": 5}
    return job_id


def test_admin_requires_token(client):
    assert client.get("/admin/dlq").status_code == 401
    r = client.get("/admin/dlq", headers={"X-Admin-Token": "wrong"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "UNAUTHORIZED"
    assert client.post(f"/admin/dlq/{uuid.uuid4()}/replay").status_code == 401
    assert client.get("/admin").status_code == 401


def test_admin_accepts_basic_auth_for_browsers(client):
    basic = base64.b64encode(b"admin:test-admin-token").decode()
    assert client.get("/admin", headers={"Authorization": f"Basic {basic}"}).status_code == 200


def test_admin_not_mounted_without_token(publisher, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import create_app

    monkeypatch.setattr(get_settings(), "admin_token", "")
    with TestClient(create_app(publisher=publisher, init_infra=False)) as c:
        assert c.get("/admin/dlq", headers=AUTH).status_code == 404


def test_listing_shows_dead_lettered_jobs(client, publisher):
    job_id = dead_lettered_job(client, publisher)
    r = client.get("/admin/dlq", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["dlq_messages"] == 1
    row = next(j for j in body["jobs"] if j["job_id"] == job_id)
    assert row["error"]["code"] == "VIDEO_UNAVAILABLE"
    assert row["attempts"] == 5
    assert job_id in client.get("/admin", headers=AUTH).text


def test_replay_requeues_and_republishes(client, publisher):
    job_id = dead_lettered_job(client, publisher)
    r = client.post(f"/admin/dlq/{job_id}/replay", headers=AUTH)
    assert r.status_code == 202
    assert publisher.dlq == []
    assert publisher.published[-1]["job_id"] == job_id
    job = client.get(f"/jobs/{job_id}").json()
    assert job["status"] == "QUEUED"
    assert job["attempt"] == 1
    assert job["error"] is None


def test_replay_non_dlq_job_is_409(client):
    job_id = client.post("/jobs", json={"youtube_url": URL, "notion_token": "secret_test"}).json()["job_id"]
    r = client.post(f"/admin/dlq/{job_id}/replay", headers=AUTH)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "JOB_NOT_DEAD_LETTERED"


def test_replay_without_dlq_message_is_409_and_keeps_state(client, publisher):
    job_id = dead_lettered_job(client, publisher, in_dlq=False)
    r = client.post(f"/admin/dlq/{job_id}/replay", headers=AUTH)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "DLQ_MESSAGE_NOT_FOUND"
    assert client.get(f"/jobs/{job_id}").json()["status"] == "DEAD_LETTERED"


def test_replay_unknown_job_is_404(client):
    assert client.post(f"/admin/dlq/{uuid.uuid4()}/replay", headers=AUTH).status_code == 404
