"""Stage 1 - transcript extraction (also learns the true duration and enforces Constraint A).

Captions come from YouTube when available. Videos without captions fall back to local
speech-to-text: the audio track is downloaded and transcribed with Whisper.
"""

from __future__ import annotations

import shutil
import tempfile
import uuid

from notes_common.config import get_settings
from notes_common.errors import PermanentError

from .. import asr, jobstore, youtube
from ..timefmt import mmss


def _speech_to_text(job_id: uuid.UUID, video_id: str, duration: float) -> tuple[list[dict], str]:
    workdir = tempfile.mkdtemp(prefix=f"vnt-{job_id.hex[:8]}-")
    try:
        jobstore.set_progress(job_id, 19, "No captions on this video - downloading the audio")
        path = youtube.download_audio(video_id, workdir)
        jobstore.set_progress(job_id, 21, "No captions - transcribing the audio with speech recognition")

        last = {"pct": -1}

        def progress(frac: float) -> None:
            pct = int(frac * 100)
            if pct >= last["pct"] + 5:  # throttle DB writes
                last["pct"] = pct
                jobstore.set_progress(
                    job_id, 21 + int(8 * frac),
                    f"No captions - transcribing the audio ({mmss(frac * duration)} of {mmss(duration)})",
                )

        return asr.transcribe_file(path, duration, progress)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


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
        try:
            segments, language = youtube.fetch_transcript(video_id)
            source = "captions"
        except PermanentError as exc:
            if exc.code != "NO_TRANSCRIPT" or not s.asr_fallback:
                raise
            segments, language = _speech_to_text(job_id, video_id, float(meta["duration_sec"] or 0))
            source = "speech_to_text"
        jobstore.put_artifact(job_id, "transcript", {"language": language, "source": source, "segments": segments})
