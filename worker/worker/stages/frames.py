"""Stage 3 - frame extraction (Constraint B: real frames only).

For each flagged timestamp, ffmpeg seeks inside the remote video stream and captures exactly
one frame - nothing is generated or redrawn. A failed timestamp is marked failed and simply
omitted later (graceful degradation). Optionally, Claude looks at the captured pixels and
rewrites the caption so it describes what is actually visible.
"""

from __future__ import annotations

import io
import logging
import subprocess
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

from PIL import Image, ImageStat

from notes_common import chaos
from notes_common.config import get_settings
from notes_common.errors import PermanentError, RateLimited, TransientError

from .. import claude, jobstore, prompts, youtube
from ..timefmt import mmss

log = logging.getLogger(__name__)

PARALLELISM = 3


class FrameCaptureError(Exception):
    pass


def ffmpeg_frame(url: str, ts: float, timeout: int) -> bytes:
    cmd = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-ss", f"{ts:.2f}", "-i", url,
        "-frames:v", "1", "-vf", "scale='min(1280,iw)':-2",
        "-q:v", "3", "-f", "image2", "-c:v", "mjpeg", "pipe:1",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise FrameCaptureError(f"ffmpeg timed out after {timeout}s") from exc
    if proc.returncode != 0 or len(proc.stdout) < 1000:
        raise FrameCaptureError(proc.stderr.decode(errors="ignore").strip()[-300:] or "no frame decoded")
    return proc.stdout


def is_blank(jpeg: bytes) -> bool:
    img = Image.open(io.BytesIO(jpeg)).convert("L")
    stat = ImageStat.Stat(img)
    # Near-uniform image (black/white transition screen). Dark-background slides still have contrast.
    return stat.stddev[0] < 6


def capture(video_id: str, ts: float, duration: float) -> tuple[bytes, float]:
    """Capture one real frame near `ts`; retries once with a fresh stream URL and nudges
    past blank/transition frames."""
    timeout = get_settings().frame_timeout_seconds
    last_err: Exception | None = None
    for refresh in (False, True):
        try:
            url = youtube.stream_url(video_id, refresh=refresh)
            jpeg = ffmpeg_frame(url, ts, timeout)
            if is_blank(jpeg):
                alt = min(ts + 3, max(0.0, duration - 1)) if duration else ts + 3
                try:
                    alt_jpeg = ffmpeg_frame(url, alt, timeout)
                    if not is_blank(alt_jpeg):
                        return alt_jpeg, alt
                except FrameCaptureError:
                    pass
            return jpeg, ts
        except FrameCaptureError as exc:
            last_err = exc
            log.warning("frame capture at %.1fs failed (refresh=%s): %s", ts, refresh, exc)
    raise FrameCaptureError(str(last_err))


def context_around(segments: list[dict], ts: float, window: float = 25.0) -> str:
    text = " ".join(s["text"] for s in segments if ts - window <= s["start"] <= ts + window)
    return text[:1500] or "(no narration)"


def vision_caption(jpeg: bytes, ts: float, caption: str, reason: str, segments: list[dict]) -> dict:
    return claude.structured_call(
        system="You write precise, factual captions for frames captured from educational videos.",
        content=[
            claude.image_block(jpeg),
            {"type": "text", "text": prompts.frame_prompt(
                timestamp=mmss(ts), caption=caption, reason=reason, context=context_around(segments, ts))},
        ],
        schema=prompts.FRAME_SCHEMA,
        max_tokens=2000,
    )


def _process(job_id: uuid.UUID, video_id: str, frame, duration: float, segments: list[dict]) -> None:
    s = get_settings()
    if frame.status == "pending":
        if chaos.frame_should_fail(frame.timestamp_sec, frame.idx):
            jobstore.update_frame(job_id, frame.idx, status="failed", error="injected failure (chaos)")
            return
        try:
            jpeg, captured_at = capture(video_id, frame.timestamp_sec, duration)
        except FrameCaptureError as exc:
            jobstore.update_frame(job_id, frame.idx, status="failed", error=str(exc)[:500])
            return
        jobstore.update_frame(job_id, frame.idx, status="ok", image=jpeg, captured_at_sec=captured_at)
        frame.status, frame.image, frame.captured_at_sec = "ok", jpeg, captured_at

    if frame.status == "ok" and s.frame_vision_check and not frame.vision_checked:
        try:
            result = vision_caption(frame.image, frame.captured_at_sec or frame.timestamp_sec,
                                    frame.caption, frame.reason, segments)
        except (RateLimited, TransientError):
            raise
        except PermanentError as exc:
            log.warning("vision caption skipped for frame %s: %s", frame.idx, exc)
            jobstore.update_frame(job_id, frame.idx, vision_checked=1)
            return
        caption = (result.get("caption") or "").strip() or frame.caption
        reason = (result.get("reason") or "").strip() or frame.reason
        jobstore.update_frame(job_id, frame.idx, caption=caption, reason=reason, vision_checked=1)


def run(job_id: uuid.UUID, video_id: str, synthesis: dict) -> None:
    jobstore.create_frames(job_id, synthesis.get("key_frames", []))
    frames = jobstore.list_frames(job_id)
    if not frames:
        return
    meta = jobstore.get_artifact(job_id, "meta") or {}
    duration = float(meta.get("duration_sec") or 0)
    segments = (jobstore.get_artifact(job_id, "transcript") or {}).get("segments", [])

    vision = get_settings().frame_vision_check
    todo = [f for f in frames if f.status == "pending" or (f.status == "ok" and not f.vision_checked and vision)]
    total = len(frames)
    done = total - len(todo)
    jobstore.set_progress(job_id, 50 + int(30 * done / total), f"Capturing frames ({done} of {total})")

    errors: list[BaseException] = []
    with ThreadPoolExecutor(max_workers=PARALLELISM) as pool:
        futures = {pool.submit(_process, job_id, video_id, f, duration, segments): f for f in todo}
        for fut in as_completed(futures):
            try:
                fut.result()
            except BaseException as exc:  # rate limits etc. -> re-raise after the batch settles
                errors.append(exc)
            done += 1
            jobstore.set_progress(job_id, 50 + int(30 * done / total), f"Capturing frames ({done} of {total})")
    if errors:
        raise errors[0]

    for f in jobstore.list_frames(job_id):
        if f.status == "failed":
            jobstore.add_warning(
                job_id,
                {
                    "code": "FRAME_EXTRACTION_FAILED",
                    "timestamp_sec": round(f.timestamp_sec),
                    "message": f"Could not capture the frame at {mmss(f.timestamp_sec)}; it was omitted",
                },
            )
