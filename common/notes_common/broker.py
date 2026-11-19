"""RabbitMQ topology and publishing helpers.

Topology
--------
    exchange  notes (direct, durable)
      ├─ "work" ──> queue jobs.work          durable; workers consume with manual ack, prefetch=1
      └─ "dlq"  ──> queue jobs.dlq           dead-letter queue for jobs whose retry budget is exhausted
    queue jobs.delay.<N>s  (one per backoff step)
      x-message-ttl = N s, x-dead-letter-exchange = notes, routing key "work"
      -> a message parked here re-appears on jobs.work after N seconds (exponential backoff)

Every publish uses publisher confirms + mandatory routing, so a returned/nacked message raises.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Callable, Iterable

import pika
from pika.adapters.blocking_connection import BlockingChannel

from .config import get_settings

log = logging.getLogger(__name__)

EXCHANGE = "notes"
WORK_QUEUE = "jobs.work"
DLQ_QUEUE = "jobs.dlq"
WORK_KEY = "work"
DLQ_KEY = "dlq"


def delay_queue_name(seconds: int) -> str:
    return f"jobs.delay.{int(seconds)}s"


def connection_params(heartbeat: int = 30, fast: bool = False) -> pika.URLParameters:
    params = pika.URLParameters(get_settings().rabbitmq_url)
    params.heartbeat = heartbeat
    params.blocked_connection_timeout = 60
    params.connection_attempts = 1 if fast else 3   # fast = request path: give up quickly instead of retrying
    params.retry_delay = 2
    if fast:
        params.socket_timeout = 3
        params.stack_timeout = 3
    return params


def connect(heartbeat: int = 30, retries: int = 30, fast: bool = False) -> pika.BlockingConnection:
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            return pika.BlockingConnection(connection_params(heartbeat, fast))
        except Exception as exc:  # pragma: no cover - startup race
            last = exc
            log.warning("rabbitmq not ready (%s/%s): %s", attempt, retries, exc)
            if not fast:
                time.sleep(1)
    raise RuntimeError(f"rabbitmq unavailable: {last}")


def declare_topology(ch: BlockingChannel, delays: Iterable[int] | None = None) -> None:
    delays = sorted(set(delays if delays is not None else get_settings().backoff_seconds))
    ch.exchange_declare(EXCHANGE, exchange_type="direct", durable=True)
    ch.queue_declare(WORK_QUEUE, durable=True)
    ch.queue_bind(WORK_QUEUE, EXCHANGE, routing_key=WORK_KEY)
    ch.queue_declare(DLQ_QUEUE, durable=True)
    ch.queue_bind(DLQ_QUEUE, EXCHANGE, routing_key=DLQ_KEY)
    for d in delays:
        ch.queue_declare(
            delay_queue_name(d),
            durable=True,
            arguments={
                "x-message-ttl": int(d) * 1000,
                "x-dead-letter-exchange": EXCHANGE,
                "x-dead-letter-routing-key": WORK_KEY,
            },
        )


def pick_delay(wanted: float) -> int:
    """Choose the smallest configured delay queue that is >= `wanted` (or the largest one)."""
    steps = sorted(set(get_settings().backoff_seconds))
    for s in steps:
        if s >= wanted:
            return s
    return steps[-1]


def _props(attempt: int, extra_headers: dict | None = None) -> pika.BasicProperties:
    headers = {"x-attempt": attempt}
    if extra_headers:
        headers.update(extra_headers)
    return pika.BasicProperties(
        content_type="application/json",
        delivery_mode=pika.DeliveryMode.Persistent,
        headers=headers,
    )


def publish_work(ch: BlockingChannel, body: dict, attempt: int = 1) -> None:
    ch.basic_publish(EXCHANGE, WORK_KEY, json.dumps(body).encode(), _props(attempt), mandatory=True)


def publish_delayed(ch: BlockingChannel, body: dict, attempt: int, delay: int) -> None:
    # Default exchange routes directly to the delay queue by name.
    ch.basic_publish("", delay_queue_name(delay), json.dumps(body).encode(), _props(attempt), mandatory=True)


def publish_dlq(ch: BlockingChannel, body: dict, attempt: int, error: dict) -> None:
    headers = {"x-error-code": error.get("code", ""), "x-error-message": error.get("message", "")[:500]}
    ch.basic_publish(EXCHANGE, DLQ_KEY, json.dumps(body).encode(), _props(attempt, headers), mandatory=True)


class Publisher:
    """Thread-safe, lazily (re)connecting publisher used by the gateway."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._conn: pika.BlockingConnection | None = None
        self._ch: BlockingChannel | None = None
        self._down_until = 0.0

    def _ensure(self) -> BlockingChannel:
        if self._conn is None or self._conn.is_closed or self._ch is None or self._ch.is_closed:
            if time.monotonic() < self._down_until:  # broker just failed: answer instantly, don't queue behind the lock
                raise RuntimeError("rabbitmq unavailable")
            try:
                self._conn = connect(heartbeat=0, retries=1, fast=True)
            except Exception:
                self._down_until = time.monotonic() + 2
                raise
            self._ch = self._conn.channel()
            self._ch.confirm_delivery()
            declare_topology(self._ch)
        return self._ch

    def publish_job(self, body: dict) -> None:
        with self._lock:
            for attempt in (1, 2):
                try:
                    publish_work(self._ensure(), body, attempt=1)
                    return
                except (pika.exceptions.AMQPError, OSError) as exc:
                    log.warning("publish failed (try %s): %s", attempt, exc)
                    self.close()
            raise RuntimeError("could not publish job to broker")

    def queue_stats(self) -> dict:
        with self._lock:
            ch = self._ensure()
            work = ch.queue_declare(WORK_QUEUE, durable=True, passive=True)
            dlq = ch.queue_declare(DLQ_QUEUE, durable=True, passive=True)
            return {
                "work_ready": work.method.message_count,
                "consumers": work.method.consumer_count,
                "dead_lettered": dlq.method.message_count,
            }

    def queue_depths(self) -> dict[str, int]:
        """Ready-message count of jobs.work, every delay queue and jobs.dlq."""
        names = [WORK_QUEUE, *(delay_queue_name(d) for d in sorted(set(get_settings().backoff_seconds))), DLQ_QUEUE]
        with self._lock:
            ch = self._ensure()
            return {n: ch.queue_declare(n, passive=True).method.message_count for n in names}

    def replay_from_dlq(self, job_id: str, before_publish: Callable[[], None]) -> bool:
        """Move the DLQ message for `job_id` back onto jobs.work (as attempt 1).

        Browsed messages stay unacked until the end, so `basic_get` walks the queue once and
        everything we don't take is requeued in place. `before_publish` runs (e.g. resetting the
        job row) only once the message is found; if it or the publish raises, nothing is acked.
        Returns False if no DLQ message matches.
        """
        with self._lock:
            ch = self._ensure()
            held = False
            try:
                while True:
                    method, _props, body = ch.basic_get(DLQ_QUEUE, auto_ack=False)
                    if method is None:
                        return False
                    held = True
                    try:
                        message = json.loads(body)
                    except ValueError:
                        continue
                    if message.get("job_id") == job_id:
                        before_publish()
                        publish_work(ch, message, attempt=1)
                        ch.basic_ack(method.delivery_tag)
                        return True
            finally:
                if held and ch.is_open:
                    ch.basic_nack(delivery_tag=0, multiple=True, requeue=True)

    def close(self) -> None:
        try:
            if self._conn and self._conn.is_open:
                self._conn.close()
        except Exception:
            pass
        self._conn = None
        self._ch = None
