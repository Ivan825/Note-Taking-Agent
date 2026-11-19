"""Dead-letter queue admin: inspect DEAD_LETTERED jobs and replay them (mounted only if ADMIN_TOKEN is set)."""

from __future__ import annotations

import base64
import binascii
import html
import logging
import secrets

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from notes_common.config import get_settings
from notes_common.db import session_scope
from notes_common.models import Job
from notes_common.states import PROGRESS, Status

from ..errors import ApiError
from .jobs import _parse_job_id

log = logging.getLogger(__name__)


def require_admin(request: Request) -> None:
    """X-Admin-Token header, or HTTP Basic with the token as password (so /admin opens in a browser)."""
    expected = get_settings().admin_token
    given = request.headers.get("x-admin-token", "")
    auth = request.headers.get("authorization", "")
    if not given and auth.lower().startswith("basic "):
        try:
            given = base64.b64decode(auth[6:]).decode().partition(":")[2]
        except (binascii.Error, UnicodeDecodeError):
            given = ""
    if not (expected and given and secrets.compare_digest(given.encode(), expected.encode())):
        raise ApiError(401, "UNAUTHORIZED", "Missing or invalid X-Admin-Token",
                       headers={"WWW-Authenticate": 'Basic realm="admin"'})


router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])


def _row(job: Job) -> dict:
    err = job.error or {}
    return {
        "job_id": str(job.id),
        "video_id": job.video_id,
        "video_title": job.video_title,
        "error": {"code": err.get("code"), "message": err.get("message")},
        "attempts": err.get("attempts", job.attempt),
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }


def _dlq_listing(request: Request) -> dict:
    with session_scope() as db:
        jobs = db.scalars(
            select(Job).where(Job.status == Status.DEAD_LETTERED.value).order_by(Job.finished_at.desc())
        ).all()
        rows = [_row(j) for j in jobs]
    try:
        messages = request.app.state.publisher.queue_stats()["dead_lettered"]
    except Exception as exc:  # broker down: the DB listing is still useful
        log.warning("could not read jobs.dlq depth: %s", exc)
        messages = None
    return {"dlq_messages": messages, "jobs": rows}


@router.get("/dlq")
def list_dlq(request: Request):
    return _dlq_listing(request)


def requeue_dead_lettered(job_id) -> None:
    """The one sanctioned exit from a terminal state: DEAD_LETTERED -> QUEUED for an admin replay.

    Deliberately bypasses `can_transition`. Artifacts and frames are kept so the job resumes
    from its last checkpoint.
    """
    with session_scope() as db:
        job = db.get(Job, job_id, with_for_update=True)
        if job is None or job.status != Status.DEAD_LETTERED.value:
            raise ApiError(409, "JOB_NOT_DEAD_LETTERED", "Job is no longer dead-lettered")
        job.status = Status.QUEUED.value
        job.status_detail = "Replayed from the dead-letter queue"
        job.progress = PROGRESS[Status.QUEUED]
        job.attempt = 1
        job.error = None
        job.next_retry_at = None
        job.finished_at = None


@router.post("/dlq/{job_id}/replay", status_code=202)
def replay(job_id: str, request: Request):
    jid = _parse_job_id(job_id)
    with session_scope() as db:
        job = db.get(Job, jid)
        if job is None:
            raise ApiError(404, "JOB_NOT_FOUND", f"No job with id '{job_id}'")
        if job.status != Status.DEAD_LETTERED.value:
            raise ApiError(409, "JOB_NOT_DEAD_LETTERED",
                           f"Job is {job.status}; only DEAD_LETTERED jobs can be replayed")
    found = request.app.state.publisher.replay_from_dlq(str(jid), lambda: requeue_dead_lettered(jid))
    if not found:
        raise ApiError(409, "DLQ_MESSAGE_NOT_FOUND",
                       "The job's message is no longer in jobs.dlq, so it can't be replayed")
    log.info("job %s replayed from the DLQ", jid)
    return {"job_id": str(jid), "status": Status.QUEUED.value}


@router.get("", response_class=HTMLResponse, include_in_schema=False)
def admin_page(request: Request):
    data = _dlq_listing(request)
    esc = html.escape
    rows = "".join(
        f"<tr><td><code>{esc(j['job_id'])}</code></td>"
        f"<td>{esc(j['video_title'] or j['video_id'])}</td>"
        f"<td><b>{esc(j['error']['code'] or '')}</b> {esc(j['error']['message'] or '')}</td>"
        f"<td>{esc(str(j['attempts']))}</td><td>{esc(j['finished_at'] or '')}</td>"
        f"<td><button data-id=\"{esc(j['job_id'])}\">Replay</button></td></tr>"
        for j in data["jobs"]
    ) or '<tr><td colspan="6">The dead-letter queue is empty.</td></tr>'
    depth = "unknown (broker unreachable)" if data["dlq_messages"] is None else data["dlq_messages"]
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>DLQ admin</title>
<style>
  body {{ font: 14px system-ui, sans-serif; margin: 24px; }}
  table {{ border-collapse: collapse; width: 100%; }}
  th, td {{ border-bottom: 1px solid #ddd; padding: 6px 8px; text-align: left; vertical-align: top; }}
</style></head><body>
<h1>Dead-letter queue</h1>
<p>Messages in <code>jobs.dlq</code>: <b>{depth}</b></p>
<table><thead><tr><th>Job</th><th>Video</th><th>Error</th><th>Attempts</th><th>Finished</th><th></th></tr></thead>
<tbody>{rows}</tbody></table>
<script>
document.querySelectorAll("button[data-id]").forEach((b) => b.addEventListener("click", async () => {{
  b.disabled = true;
  const r = await fetch(`/admin/dlq/${{b.dataset.id}}/replay`, {{ method: "POST" }});
  const body = await r.json().catch(() => ({{}}));
  if (!r.ok) alert((body.error && body.error.message) || `Replay failed (${{r.status}})`);
  location.reload();
}}));
</script></body></html>"""
