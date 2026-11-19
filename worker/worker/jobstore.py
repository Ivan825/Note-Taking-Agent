"""Small data-access layer over the job tables. Every write commits immediately, so each
completed unit of work is a durable checkpoint before the message is acknowledged."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select

from notes_common.db import session_scope
from notes_common.models import Artifact, Frame, Job, utcnow
from notes_common.states import PROGRESS, InvalidTransition, Status, can_transition


def get_job(job_id: uuid.UUID) -> Job | None:
    with session_scope() as db:
        return db.get(Job, job_id)


def update_job(job_id: uuid.UUID, **fields: Any) -> None:
    with session_scope() as db:
        job = db.get(Job, job_id)
        if job is None:
            return
        for k, v in fields.items():
            setattr(job, k, v)
        job.updated_at = utcnow()


def transition(job_id: uuid.UUID, new: Status, detail: str | None = None, progress: int | None = None, **fields: Any) -> None:
    """Move the job to `new`, enforcing the state machine (spec section 4)."""
    with session_scope() as db:
        job = db.get(Job, job_id, with_for_update=True)
        if job is None:
            return
        current = Status(job.status)
        if not can_transition(current, new):
            raise InvalidTransition(f"{current.value} -> {new.value}")
        job.status = new.value
        if detail is not None:
            job.status_detail = detail
        job.progress = progress if progress is not None else max(job.progress or 0, PROGRESS[new])
        job.next_retry_at = None
        for k, v in fields.items():
            setattr(job, k, v)
        job.updated_at = utcnow()


def set_progress(job_id: uuid.UUID, progress: int, detail: str | None = None) -> None:
    fields: dict[str, Any] = {"progress": progress}
    if detail is not None:
        fields["status_detail"] = detail
    update_job(job_id, **fields)


def add_warning(job_id: uuid.UUID, warning: dict) -> None:
    with session_scope() as db:
        job = db.get(Job, job_id, with_for_update=True)
        if job is None:
            return
        existing = list(job.warnings or [])
        key = (warning.get("code"), warning.get("timestamp_sec"))
        if all((w.get("code"), w.get("timestamp_sec")) != key for w in existing):
            existing.append(warning)
        job.warnings = existing


# ---------------------------------------------------------------- artifacts
def get_artifact(job_id: uuid.UUID, kind: str) -> dict | None:
    with session_scope() as db:
        a = db.get(Artifact, (job_id, kind))
        return a.data if a else None


def put_artifact(job_id: uuid.UUID, kind: str, data: dict) -> None:
    with session_scope() as db:
        a = db.get(Artifact, (job_id, kind))
        if a:
            a.data = data
        else:
            db.add(Artifact(job_id=job_id, kind=kind, data=data))


# ---------------------------------------------------------------- frames
def list_frames(job_id: uuid.UUID) -> list[Frame]:
    with session_scope() as db:
        return list(db.scalars(select(Frame).where(Frame.job_id == job_id).order_by(Frame.idx)))


def create_frames(job_id: uuid.UUID, key_frames: list[dict]) -> None:
    """Insert one row per flagged timestamp (no-op if rows already exist - idempotent)."""
    with session_scope() as db:
        exists = db.scalars(select(Frame.idx).where(Frame.job_id == job_id).limit(1)).first()
        if exists is not None:
            return
        for i, kf in enumerate(key_frames):
            db.add(
                Frame(
                    job_id=job_id,
                    idx=i,
                    timestamp_sec=float(kf["timestamp_sec"]),
                    caption=kf["caption"],
                    reason=kf["reason"],
                    status="pending",
                )
            )


def update_frame(job_id: uuid.UUID, idx: int, **fields: Any) -> None:
    with session_scope() as db:
        f = db.get(Frame, (job_id, idx))
        if f is None:
            return
        for k, v in fields.items():
            setattr(f, k, v)
