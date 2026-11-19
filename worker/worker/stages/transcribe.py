"""Stage 1 - transcript extraction (also learns the true duration and enforces Constraint A)."""

from __future__ import annotations

import uuid

from notes_common.config import get_settings
from notes_common.errors import PermanentError

from .. import jobstore, youtube
from ..timefmt import mmss


def run(job_id: uuid.UUID, video_id: str) -> None:
    s = get_settings()

    meta = jobstore.get_artifact(job_id, "meta")
    if meta is None:
        jobstore.set_progress(job_id, 12, "Reading video details")
        meta = youtube.fetch_metadata(video_id)
        jobstore.put_artifact(job_id, "meta", meta)
    jobstore.update_job(job_id, video_title=meta["title"], duration_sec=meta["duration_sec"] or None)

    # Constraint A: deterministic rejection of anything longer than the cap.
    if meta["duration_sec"] > s.max_video_seconds:
        raise PermanentError(
            f"Video is {mmss(meta['duration_sec'])} long; the limit is {mmss(s.max_video_seconds)}",
            "VIDEO_TOO_LONG",
        )

    if jobstore.get_artifact(job_id, "transcript") is None:
        jobstore.set_progress(job_id, 18, "Downloading the transcript")
        segments, language = youtube.fetch_transcript(video_id)
        jobstore.put_artifact(job_id, "transcript", {"language": language, "segments": segments})
