"""GET /metrics and stage timings (Task 6)."""

import uuid

from app.routes import metrics
from notes_common.db import session_scope
from notes_common.models import Job
from notes_common.states import Status
from worker import jobstore

URL = "https://www.youtube.com/watch?v=aircAruvnKk"


class FakeRedis:
    def __init__(self, data):
        self.data = data

    def scan_iter(self, pattern):
        return [k for k in self.data if k.startswith(pattern.rstrip("*"))]

    def get(self, key):
        return self.data.get(key)


def test_metrics_exposition(client, monkeypatch):
    monkeypatch.setattr(metrics, "get_redis", lambda: FakeRedis({"metrics:ratelimit_wait:llm_requests": "12.5"}))
    job_id = client.post("/jobs", json={"youtube_url": URL, "notion_token": "t"}).json()["job_id"]
    with session_scope() as db:
        db.get(Job, uuid.UUID(job_id)).stage_timings = {
            "TRANSCRIBING": {"started_at": "2026-01-01T00:00:00+00:00", "finished_at": "2026-01-01T00:00:04+00:00"},
            "SUMMARIZING": {"started_at": "2026-01-01T00:00:04+00:00"},  # still running: not observed
        }

    r = client.get("/metrics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    text = r.text
    assert 'jobs_total{status="QUEUED"}' in text
    assert 'jobs_total{status="DEAD_LETTERED"}' in text
    assert 'queue_depth{queue="jobs.work"}' in text
    assert 'ratelimit_wait_seconds_total{bucket="llm_requests"} 12.5' in text
    assert 'stage_duration_seconds_bucket{le="5",stage="TRANSCRIBING"}' in text
    assert 'stage_duration_seconds_count{stage="SUMMARIZING"} 0.0' in text


def test_stage_durations_and_buckets():
    d = metrics.stage_durations([
        {"TRANSCRIBING": {"started_at": "2026-01-01T00:00:00+00:00", "finished_at": "2026-01-01T00:00:04+00:00"}},
        {"TRANSCRIBING": {"started_at": "2026-01-01T00:00:00+00:00", "finished_at": "2026-01-01T00:01:30+00:00"}},
    ])
    assert d["TRANSCRIBING"] == [4.0, 90.0]
    h = metrics.histogram("x", "x", {"TRANSCRIBING": d["TRANSCRIBING"]})
    buckets = {s.labels["le"]: s.value for s in h.samples if s.name == "x_bucket"}
    assert buckets["5"] == 1 and buckets["120"] == 2 and buckets["+Inf"] == 2


def test_mark_stage_keeps_first_value(client):
    job_id = uuid.UUID(client.post("/jobs", json={"youtube_url": URL, "notion_token": "t"}).json()["job_id"])
    jobstore.mark_stage(job_id, Status.TRANSCRIBING, "started_at")
    first = jobstore.get_job(job_id).stage_timings["TRANSCRIBING"]["started_at"]
    jobstore.mark_stage(job_id, Status.TRANSCRIBING, "started_at")  # redelivery
    jobstore.mark_stage(job_id, Status.TRANSCRIBING, "finished_at")
    t = jobstore.get_job(job_id).stage_timings["TRANSCRIBING"]
    assert t["started_at"] == first and "finished_at" in t
