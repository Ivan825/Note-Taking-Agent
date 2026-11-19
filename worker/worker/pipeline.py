"""Runs one job message through the four stages, resuming from the last durable checkpoint.

The function never acknowledges anything itself; it returns an Outcome that the consumer
turns into broker actions (ack / delayed retry / dead-letter) on the connection thread.
"""

from __future__ import annotations

import logging
import socket
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from notes_common import broker, crypto
from notes_common.config import get_settings
from notes_common.errors import PermanentError, PipelineError, RateLimited, TransientError
from notes_common.states import InvalidTransition, Status, is_terminal, stage_index

from . import jobstore
from .stages import frames, publish, synthesize, transcribe

log = logging.getLogger(__name__)
WORKER_ID = socket.gethostname()

STAGE_DETAIL = {
    Status.TRANSCRIBING: "Fetching video details and transcript",
    Status.SUMMARIZING: "Writing notes with Claude",
    Status.EXTRACTING_FRAMES: "Capturing frames from the video",
    Status.PUBLISHING: "Publishing to Notion",
}


@dataclass
class Outcome:
    kind: str  # ack | retry | dead
    reason: str = ""
    delay: int = 0
    next_attempt: int = 0
    error: dict = field(default_factory=dict)
    throttled: bool = False  # re-queued only to wait for our shared rate-limit budget


def _enter(job_id: uuid.UUID, stage: Status, current: Status) -> Status:
    jobstore.mark_stage(job_id, stage, "started_at")
    if stage_index(current) > stage_index(stage):
        return current
    if current != stage:
        jobstore.transition(job_id, stage, STAGE_DETAIL[stage])
    else:
        jobstore.update_job(job_id, status_detail=STAGE_DETAIL[stage], next_retry_at=None)
    return stage


def run(message: dict, attempt: int, redelivered: bool) -> Outcome:
    try:
        job_id = uuid.UUID(message["job_id"])
    except (KeyError, ValueError, TypeError):
        log.error("dropping malformed message: %r", message)
        return Outcome("ack", "malformed message")

    job = jobstore.get_job(job_id)
    if job is None:
        log.warning("job %s not found; dropping message", job_id)
        return Outcome("ack", "unknown job")
    if is_terminal(job.status):
        log.info("job %s already %s; dropping duplicate delivery", job_id, job.status)
        return Outcome("ack", "already finished")
    if attempt < (job.attempt or 1):
        log.info("job %s: stale delivery (attempt %s < %s); dropping", job_id, attempt, job.attempt)
        return Outcome("ack", "stale attempt")

    fields = {"worker_id": WORKER_ID, "attempt": attempt}
    if job.started_at is None:
        fields["started_at"] = datetime.now(timezone.utc)
    if redelivered and Status(job.status) != Status.QUEUED:
        fields["recoveries"] = (job.recoveries or 0) + 1
        log.warning("job %s: redelivered after a worker failure; resuming at %s on %s", job_id, job.status, WORKER_ID)
    jobstore.update_job(job_id, **fields)

    current = Status(job.status)
    stage = current if current != Status.QUEUED else Status.TRANSCRIBING
    try:
        token = crypto.decrypt(message["notion_token"])
        video_id = message.get("video_id") or job.video_id

        current = _enter(job_id, Status.TRANSCRIBING, current)
        stage = Status.TRANSCRIBING
        transcribe.run(job_id, video_id)
        jobstore.mark_stage(job_id, Status.TRANSCRIBING, "finished_at")

        current = _enter(job_id, Status.SUMMARIZING, current)
        stage = Status.SUMMARIZING
        synthesis = synthesize.run(job_id)
        jobstore.mark_stage(job_id, Status.SUMMARIZING, "finished_at")

        current = _enter(job_id, Status.EXTRACTING_FRAMES, current)
        stage = Status.EXTRACTING_FRAMES
        frames.run(job_id, video_id, synthesis)
        jobstore.mark_stage(job_id, Status.EXTRACTING_FRAMES, "finished_at")

        current = _enter(job_id, Status.PUBLISHING, current)
        stage = Status.PUBLISHING
        result = publish.run(job_id, video_id, token, message.get("notion_parent_id") or job.notion_parent_id)
        jobstore.mark_stage(job_id, Status.PUBLISHING, "finished_at")

        final = (
            Status.COMPLETED
            if result["frames_published"] == result["frames_requested"]
            else Status.COMPLETED_WITH_WARNINGS
        )
        public_result = {k: result[k] for k in ("notion_page_url", "frames_requested", "frames_published")}
        detail = "Done" if final == Status.COMPLETED else (
            f"Published with {result['frames_published']} of {result['frames_requested']} frames"
        )
        jobstore.transition(job_id, final, detail, 100, result=public_result,
                            finished_at=datetime.now(timezone.utc), error=None)
        log.info("job %s %s -> %s", job_id, final.value, public_result["notion_page_url"])
        return Outcome("ack", final.value)

    except PermanentError as exc:
        log.warning("job %s failed permanently at %s: %s", job_id, stage.value, exc.message)
        _fail(job_id, Status.FAILED, {**exc.to_dict(), "stage": stage.value}, exc.message)
        return Outcome("ack", "failed")
    except (RateLimited, TransientError) as exc:
        return _retry_or_dead(job_id, stage, attempt, exc)
    except InvalidTransition as exc:
        log.error("job %s: invalid transition %s; dropping", job_id, exc)
        return Outcome("ack", "invalid transition")
    except Exception as exc:  # unexpected bug or infrastructure hiccup: retry, bounded by the budget
        log.exception("job %s: unexpected error at %s", job_id, stage.value)
        err = TransientError(f"Unexpected error: {type(exc).__name__}: {exc}"[:300], "INTERNAL_ERROR")
        return _retry_or_dead(job_id, stage, attempt, err)


def _fail(job_id: uuid.UUID, status: Status, error: dict, detail: str) -> None:
    try:
        jobstore.transition(job_id, status, detail, 100, error=error, finished_at=datetime.now(timezone.utc))
    except InvalidTransition:
        pass


def _retry_or_dead(job_id: uuid.UUID, stage: Status, attempt: int, exc: PipelineError) -> Outcome:
    s = get_settings()
    if isinstance(exc, RateLimited) and exc.throttled:
        # Waiting for our shared rate-limit budget is not a failure: re-queue without using up an attempt,
        # otherwise a big burst would dead-letter perfectly healthy jobs.
        delay = broker.pick_delay(exc.retry_after or 1)
        log.info("job %s: shared %s budget is busy at %s - re-queued in %ss (attempt %s kept)",
                 job_id, exc.api, stage.value, delay, attempt)
        return Outcome("retry", exc.message, delay=delay, next_attempt=attempt, error=exc.to_dict(),
                       throttled=True)
    next_attempt = attempt + 1
    if next_attempt > s.max_attempts:
        log.warning("job %s: retry budget exhausted after %s attempts (%s)", job_id, attempt, exc.message)
        error = {**exc.to_dict(), "attempts": attempt, "stage": stage.value}
        return Outcome("dead", exc.message, error=error, next_attempt=attempt)
    delay = s.backoff_for(attempt)
    if isinstance(exc, RateLimited) and exc.retry_after:
        delay = max(delay, int(exc.retry_after + 0.999))
    delay = broker.pick_delay(delay)
    log.info("job %s: %s at %s - retry %s/%s in %ss",
             job_id, exc.message, stage.value, next_attempt, s.max_attempts, delay)
    return Outcome("retry", exc.message, delay=delay, next_attempt=next_attempt, error=exc.to_dict())


# ------------------------------------------------------------------ called after the broker confirmed
def record_retry(job_id: uuid.UUID, outcome: Outcome) -> None:
    s = get_settings()
    detail = (f"{outcome.reason} - retrying in {outcome.delay}s (attempt {outcome.next_attempt} of {s.max_attempts})")
    if outcome.throttled:
        api = outcome.error["code"].split("_")[0].title()
        detail = f"Waiting for the shared {api} rate limit - retrying in {outcome.delay}s"
    jobstore.update_job(
        job_id,
        attempt=outcome.next_attempt,
        status_detail=detail,
        next_retry_at=datetime.now(timezone.utc) + timedelta(seconds=outcome.delay),
    )


def record_dead(job_id: uuid.UUID, outcome: Outcome) -> None:
    _fail(job_id, Status.DEAD_LETTERED, outcome.error,
          f"Gave up after {outcome.next_attempt} attempts: {outcome.reason}")
