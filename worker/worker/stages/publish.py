"""Stage 4 - Notion assembly.

Idempotent by construction, so a redelivered message can never produce a duplicate page:
  1. each frame is uploaded once; its upload id is checkpointed (re-uploaded only if it expired);
  2. before creating, look for a page already created for this job (title tag "<job8>");
  3. the whole page is created in ONE request and its id is saved immediately.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from notes_common.config import get_settings
from notes_common.errors import PermanentError
from notes_common.notion_api import NotionClient

from .. import jobstore, notion_blocks


def _job_tag(job_id: uuid.UUID) -> str:
    return job_id.hex[:8]


def run(job_id: uuid.UUID, video_id: str, notion_token: str, parent_id: str | None) -> dict:
    s = get_settings()
    job = jobstore.get_job(job_id)
    meta = jobstore.get_artifact(job_id, "meta") or {}
    synthesis = jobstore.get_artifact(job_id, "synthesis") or {}
    frames = jobstore.list_frames(job_id)
    frames_requested = len(frames)
    video_url = f"https://www.youtube.com/watch?v={video_id}"

    client = NotionClient(notion_token)
    try:
        if job and job.notion_page_id:
            page_id, page_url = job.notion_page_id, job.notion_page_url
        else:
            if not parent_id:
                jobstore.set_progress(job_id, 86, "Finding your Notion notes page")
                parent_id = client.ensure_default_page(s.default_notion_page_title)
                if not parent_id:
                    raise PermanentError(
                        "No Notion page is shared with the Video Note Taker connection"
                        " - reconnect Notion and pick a page",
                        "NOTION_NO_DESTINATION",
                    )
                jobstore.update_job(job_id, notion_parent_id=parent_id)
            parent = client.resolve_parent(parent_id)

            tag = _job_tag(job_id)
            existing = client.find_child_page_with_tag(parent, tag)  # also matches older "#tag" titles
            if existing:
                page_id = existing["id"]
                page_url = existing.get("url") or f"https://www.notion.so/{page_id.replace('-', '')}"
            else:
                ok_frames = [f for f in frames if f.status == "ok" and f.image]
                fresh_before = datetime.now(timezone.utc) - timedelta(minutes=50)
                published: list[dict] = []
                for n, f in enumerate(ok_frames, start=1):
                    jobstore.set_progress(job_id, 86 + int(8 * n / max(1, len(ok_frames))),
                                          f"Uploading frames to Notion ({n} of {len(ok_frames)})")
                    upload_id = f.notion_upload_id
                    uploaded_at = f.uploaded_at
                    if uploaded_at is not None and uploaded_at.tzinfo is None:
                        uploaded_at = uploaded_at.replace(tzinfo=timezone.utc)
                    if not upload_id or not uploaded_at or uploaded_at < fresh_before:
                        upload_id = client.upload_image(f"{video_id}_{int(f.timestamp_sec)}s.jpg", f.image)
                        jobstore.update_frame(job_id, f.idx, notion_upload_id=upload_id,
                                              uploaded_at=datetime.now(timezone.utc))
                    published.append({
                        "timestamp_sec": f.captured_at_sec or f.timestamp_sec,
                        "caption": f.caption,
                        "reason": f.reason,
                        "upload_id": upload_id,
                    })

                jobstore.set_progress(job_id, 95, "Creating the Notion page")
                title = notion_blocks.page_title(synthesis.get("title") or meta.get("title") or video_id, tag)
                parent_obj = {k: v for k, v in parent.items() if not k.startswith("_")}
                title_prop = parent.get("_title_prop", "title")
                body = {
                    "parent": parent_obj,
                    "icon": {"type": "emoji", "emoji": "📝"},
                    "properties": {title_prop: {"title": [{"type": "text", "text": {"content": title}}]}},
                    "children": notion_blocks.build_children(
                        meta=meta, synthesis=synthesis, frames=published, video_url=video_url,
                        frames_requested=frames_requested,
                        transcript_source=(jobstore.get_artifact(job_id, "transcript") or {}).get("source", "captions"),
                    ),
                }
                if meta.get("thumbnail"):
                    body["cover"] = {"type": "external", "external": {"url": meta["thumbnail"]}}
                page = client.create_page(body)
                page_id = page["id"]
                page_url = page.get("url") or f"https://www.notion.so/{page_id.replace('-', '')}"
            jobstore.update_job(job_id, notion_page_id=page_id, notion_page_url=page_url)
    finally:
        client.close()

    frames_published = sum(1 for f in jobstore.list_frames(job_id) if f.status == "ok")
    return {
        "notion_page_url": page_url,
        "notion_page_id": page_id,
        "frames_requested": frames_requested,
        "frames_published": frames_published,
    }
