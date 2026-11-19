"""GET /metrics - Prometheus text format, computed from shared state at scrape time.

Everything is read from Postgres / RabbitMQ / Redis on each scrape (not from in-process
counters), so the numbers are correct no matter how many gateway or worker replicas run.
"""

from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily, HistogramMetricFamily
from sqlalchemy import func, select

from notes_common.db import session_scope
from notes_common.models import Job
from notes_common.ratelimit import WAIT_KEY_PREFIX
from notes_common.redis_client import get_redis
from notes_common.states import PIPELINE, Status

log = logging.getLogger(__name__)
router = APIRouter(tags=["metrics"])

BUCKETS = (1, 2, 5, 10, 20, 30, 60, 120, 300, 600)


def stage_durations(timings: list[dict]) -> dict[str, list[float]]:
    out: dict[str, list[float]] = {s.value: [] for s in PIPELINE}
    for t in timings:
        for stage, mark in (t or {}).items():
            if stage in out and mark.get("started_at") and mark.get("finished_at"):
                d = datetime.fromisoformat(mark["finished_at"]) - datetime.fromisoformat(mark["started_at"])
                out[stage].append(max(0.0, d.total_seconds()))
    return out


def histogram(name: str, doc: str, values_by_stage: dict[str, list[float]]) -> HistogramMetricFamily:
    h = HistogramMetricFamily(name, doc, labels=["stage"])
    for stage, values in values_by_stage.items():
        buckets = [(str(b), sum(v <= b for v in values)) for b in BUCKETS] + [("+Inf", len(values))]
        h.add_metric([stage], buckets, sum(values))
    return h


class Collector:
    def __init__(self, publisher) -> None:
        self.publisher = publisher

    def collect(self):
        with session_scope() as db:
            counts = dict(db.execute(select(Job.status, func.count()).group_by(Job.status)).all())
            timings = list(db.scalars(select(Job.stage_timings).where(Job.stage_timings.is_not(None))))

        jobs = GaugeMetricFamily("jobs_total", "Jobs by current status", labels=["status"])
        for s in Status:
            jobs.add_metric([s.value], counts.get(s.value, 0))
        yield jobs

        depth = GaugeMetricFamily("queue_depth", "Ready messages per RabbitMQ queue", labels=["queue"])
        try:
            for queue, n in self.publisher.queue_depths().items():
                depth.add_metric([queue], n)
        except Exception as exc:  # broker down: still serve the DB metrics
            log.warning("queue_depth unavailable: %s", exc)
        yield depth

        yield histogram("stage_duration_seconds", "Wall-clock time per pipeline stage (incl. retry waits)",
                        stage_durations(timings))

        waits = CounterMetricFamily("ratelimit_wait_seconds", "Seconds workers slept on a rate-limit bucket",
                                    labels=["bucket"])
        try:
            r = get_redis()
            for key in r.scan_iter(f"{WAIT_KEY_PREFIX}*"):
                waits.add_metric([key[len(WAIT_KEY_PREFIX):]], float(r.get(key) or 0))
        except Exception as exc:
            log.warning("ratelimit_wait_seconds unavailable: %s", exc)
        yield waits


@router.get("/metrics", include_in_schema=False)
def metrics(request: Request):
    return Response(generate_latest(Collector(request.app.state.publisher)), media_type=CONTENT_TYPE_LATEST)
