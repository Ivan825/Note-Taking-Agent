"""Worker process: consumes jobs from RabbitMQ with manual acks (at-least-once delivery).

* prefetch=1 - a worker holds at most one unacknowledged job.
* The pipeline runs in a separate thread so the connection keeps sending heartbeats; if the
  process is killed (even SIGKILL) the broker notices the dead connection and redelivers the
  unacked message to a healthy worker, which resumes from the last checkpoint in Postgres.
* The message is acked only after the job is durably finished, or after its retry / DLQ copy
  has been confirmed by the broker.
"""

from __future__ import annotations

import functools
import json
import logging
import signal
import threading
import uuid

from notes_common import broker, db
from notes_common.logging_setup import setup_logging

from . import pipeline

log = logging.getLogger("worker")


class Consumer:
    def __init__(self) -> None:
        self.conn = broker.connect(heartbeat=30)
        self.ch = self.conn.channel()
        self.ch.confirm_delivery()
        broker.declare_topology(self.ch)
        self.ch.basic_qos(prefetch_count=1)
        self.ch.basic_consume(broker.WORK_QUEUE, self.on_message, auto_ack=False)
        self._busy = threading.Event()

    # broker thread -------------------------------------------------------------------
    def on_message(self, ch, method, props, body):
        attempt = int((props.headers or {}).get("x-attempt", 1))
        self._busy.set()
        threading.Thread(
            target=self._work,
            args=(method.delivery_tag, bool(method.redelivered), attempt, body),
            daemon=True,
        ).start()

    def _finish(self, tag: int, message: dict, outcome: pipeline.Outcome) -> None:
        try:
            job_id = uuid.UUID(message["job_id"]) if message.get("job_id") else None
            if outcome.kind == "retry" and job_id:
                broker.publish_delayed(self.ch, message, outcome.next_attempt, outcome.delay)
                pipeline.record_retry(job_id, outcome)
            elif outcome.kind == "dead" and job_id:
                broker.publish_dlq(self.ch, message, outcome.next_attempt, outcome.error)
                pipeline.record_dead(job_id, outcome)
            self.ch.basic_ack(tag)
        except Exception:
            # Without the ack the broker will redeliver; the job resumes from its checkpoint.
            log.exception("could not finalize delivery %s; leaving it unacked", tag)
        finally:
            self._busy.clear()

    # worker thread -------------------------------------------------------------------
    def _work(self, tag: int, redelivered: bool, attempt: int, body: bytes) -> None:
        try:
            message = json.loads(body)
        except ValueError:
            message = {}
        try:
            outcome = pipeline.run(message, attempt, redelivered)
        except Exception:
            log.exception("pipeline crashed")
            outcome = pipeline.Outcome("retry", "Worker error", delay=broker.pick_delay(10),
                                       next_attempt=attempt + 1)
        self.conn.add_callback_threadsafe(functools.partial(self._finish, tag, message, outcome))

    def run(self) -> None:
        log.info("worker %s ready, consuming %s", pipeline.WORKER_ID, broker.WORK_QUEUE)
        self.ch.start_consuming()

    def stop(self) -> None:
        def _stop():
            try:
                self.ch.stop_consuming()
            except Exception:
                pass
        self.conn.add_callback_threadsafe(_stop)


def main() -> None:
    setup_logging("worker")
    db.init_db()
    consumer = Consumer()

    def _graceful(signum, _frame):
        log.info("received signal %s; stopping (in-flight job will be redelivered)", signum)
        consumer.stop()

    signal.signal(signal.SIGTERM, _graceful)
    signal.signal(signal.SIGINT, _graceful)
    try:
        consumer.run()
    finally:
        try:
            consumer.conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
