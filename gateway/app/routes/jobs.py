"""Job API (spec section 5)."""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from notes_common import crypto
from notes_common.config import get_settings
from notes_common.db import session_scope
from notes_common.models import Job
from notes_common.states import PROGRESS, TERMINAL, Status

from ..errors import ApiError
from ..validation import canonical_url, extract_video_id

log = logging.getLogger(__name__)
router = APIRouter(tags=["jobs"])


class SubmitJob(BaseModel):
    youtube_url: str = Field(..., description="Link to a YouTube video")
    notion_token: str = Field(..., description="Notion OAuth access token obtained by the extension")
    notion_parent_id: str | None = Field(None, description="Optional Notion page or database id")
    # Optional hints from the extension (enable deterministic rejection at submission time)
    duration_sec: float | None = Field(None, ge=0)
    video_title: str | None = Field(None, max_length=500)


def _parse_job_id(job_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(job_id)
    except ValueError:
        raise ApiError(404, "JOB_NOT_FOUND", f"No job with id '{job_id}'")


def serialize(job: Job) -> dict:
    s = get_settings()
    return {
        "job_id": str(job.id),
        "status": job.status,
        "progress": job.progress,
        "video_id": job.video_id,
        "video_title": job.video_title,
        "duration_sec": job.duration_sec,
        "warnings": job.warnings or [],
        "error": job.error,
        "result": job.result,
        # extras (not required by the spec, used by the extension's UI)
        "status_detail": job.status_detail,
        "attempt": job.attempt,
        "max_attempts": s.max_attempts,
        "next_retry_at": job.next_retry_at.isoformat() if job.next_retry_at else None,
        "recoveries": job.recoveries,
        "worker_id": job.worker_id,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
    }


@router.post("/jobs", status_code=202)
def submit_job(payload: SubmitJob, request: Request):
    s = get_settings()
    video_id = extract_video_id(payload.youtube_url)
    if not video_id:
        raise ApiError(400, "INVALID_YOUTUBE_URL", "youtube_url must be a link to a YouTube video, e.g. https://www.youtube.com/watch?v=VIDEO_ID")
    token = payload.notion_token.strip()
    if not token:
        raise ApiError(400, "MISSING_NOTION_TOKEN", "notion_token is required - connect Notion in the extension first")
    if payload.duration_sec is not None and payload.duration_sec > s.max_video_seconds:
        raise ApiError(
            422,
            "VIDEO_TOO_LONG",
            f"Videos longer than {s.max_video_seconds // 60} minutes are not supported "
            f"(this one is {int(payload.duration_sec // 60)}m {int(payload.duration_sec % 60)}s)",
        )

    job = Job(
        id=uuid.uuid4(),
        video_id=video_id,
        youtube_url=canonical_url(video_id),
        status=Status.QUEUED.value,
        status_detail="Waiting for a free worker",
        progress=PROGRESS[Status.QUEUED],
        video_title=payload.video_title,
        duration_sec=payload.duration_sec,
        notion_parent_id=(payload.notion_parent_id or None),
        attempt=1,
        warnings=[],
    )
    with session_scope() as db:
        db.add(job)

    message = {
        "job_id": str(job.id),
        "video_id": video_id,
        "notion_token": crypto.encrypt(token),
        "notion_parent_id": job.notion_parent_id,
    }
    try:
        request.app.state.publisher.publish_job(message)
    except Exception as exc:
        log.exception("failed to enqueue job %s", job.id)
        with session_scope() as db:
            j = db.get(Job, job.id)
            if j:
                db.delete(j)
        raise ApiError(503, "QUEUE_UNAVAILABLE", "The job queue is unavailable - try again shortly") from exc

    log.info("job %s accepted video=%s", job.id, video_id)
    return JSONResponse(
        status_code=202,
        headers={"Location": f"/jobs/{job.id}"},
        content={"job_id": str(job.id), "status": job.status, "video_id": video_id},
    )


@router.get("/jobs/{job_id}")
def get_job(job_id: str):
    jid = _parse_job_id(job_id)
    with session_scope() as db:
        job = db.get(Job, jid)
        if job is None:
            raise ApiError(404, "JOB_NOT_FOUND", f"No job with id '{job_id}'")
        return serialize(job)


@router.get("/jobs/{job_id}/results")
def get_results(job_id: str):
    jid = _parse_job_id(job_id)
    with session_scope() as db:
        job = db.get(Job, jid)
        if job is None:
            raise ApiError(404, "JOB_NOT_FOUND", f"No job with id '{job_id}'")
        if Status(job.status) not in TERMINAL:
            raise ApiError(
                409,
                "JOB_NOT_FINISHED",
                f"Job is still {job.status}; poll GET /jobs/{job_id} until it reaches a terminal state",
                extra={"status": job.status, "progress": job.progress},
            )
        return {
            "status": job.status,
            "result": job.result,
            "warnings": job.warnings or [],
            "error": job.error,
        }
