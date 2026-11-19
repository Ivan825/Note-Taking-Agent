"""REST contract tests (spec section 5)."""

import uuid

from notes_common import crypto
from notes_common.db import session_scope
from notes_common.models import Job

URL = "https://www.youtube.com/watch?v=aircAruvnKk"


def submit(client, **overrides):
    body = {"youtube_url": URL, "notion_token": "secret_test"}
    body.update(overrides)
    return client.post("/jobs", json=body)


def test_submit_returns_202_with_location(client, publisher):
    r = submit(client)
    assert r.status_code == 202
    data = r.json()
    assert data["status"] == "QUEUED"
    assert data["video_id"] == "aircAruvnKk"
    assert r.headers["location"] == f"/jobs/{data['job_id']}"
    # enqueued, with the Notion token encrypted (never stored in the database)
    assert len(publisher.published) == 1
    msg = publisher.published[0]
    assert msg["job_id"] == data["job_id"]
    assert msg["notion_token"] != "secret_test"
    assert crypto.decrypt(msg["notion_token"]) == "secret_test"


def test_invalid_url_returns_400_error_body(client):
    r = submit(client, youtube_url="https://vimeo.com/1")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_YOUTUBE_URL"
    assert "message" in r.json()["error"]


def test_missing_token_returns_400(client):
    r = client.post("/jobs", json={"youtube_url": URL})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "MISSING_FIELD"


def test_malformed_json_returns_400(client):
    r = client.post("/jobs", content=b"not json", headers={"content-type": "application/json"})
    assert r.status_code == 400
    assert "error" in r.json()


def test_too_long_rejected_at_submission(client, publisher):
    r = submit(client, duration_sec=20 * 60 + 1)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VIDEO_TOO_LONG"
    assert publisher.published == []


def test_exactly_twenty_minutes_accepted(client):
    assert submit(client, duration_sec=1200).status_code == 202


def test_broker_down_returns_503(client, publisher):
    publisher.fail = True
    r = submit(client)
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "QUEUE_UNAVAILABLE"


def test_get_unknown_job_404(client):
    assert client.get(f"/jobs/{uuid.uuid4()}").status_code == 404
    assert client.get("/jobs/not-a-uuid").status_code == 404
    assert client.get(f"/jobs/{uuid.uuid4()}/results").status_code == 404


def test_poll_is_read_only(client):
    job_id = submit(client).json()["job_id"]
    first = client.get(f"/jobs/{job_id}").json()
    for _ in range(3):
        again = client.get(f"/jobs/{job_id}").json()
    assert again == first
    for key in ("job_id", "status", "progress", "video_title", "duration_sec", "warnings", "error", "result"):
        assert key in first


def test_results_409_until_terminal_then_200(client):
    job_id = submit(client).json()["job_id"]
    r = client.get(f"/jobs/{job_id}/results")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "JOB_NOT_FINISHED"

    with session_scope() as db:
        job = db.get(Job, uuid.UUID(job_id))
        job.status = "COMPLETED_WITH_WARNINGS"
        job.result = {"notion_page_url": "https://www.notion.so/abc", "frames_requested": 6, "frames_published": 5}
        job.warnings = [{"code": "FRAME_EXTRACTION_FAILED", "timestamp_sec": 461}]

    r = client.get(f"/jobs/{job_id}/results")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "COMPLETED_WITH_WARNINGS"
    assert body["result"]["notion_page_url"].startswith("https://")
    assert body["result"]["frames_published"] == 5
    assert body["warnings"][0]["code"] == "FRAME_EXTRACTION_FAILED"


def test_healthz(client):
    r = client.get("/healthz")
    body = r.json()
    assert body["components"]["database"]["status"] == "ok"
    assert body["components"]["broker"]["status"] == "ok"
