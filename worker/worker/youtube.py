"""YouTube access: metadata (true duration), timestamped transcript and stream URLs.

All calls go through the global `youtube` rate-limit bucket and the chaos hook, and every
failure is classified as RateLimited / TransientError / PermanentError.
"""

from __future__ import annotations

import logging

import yt_dlp
from youtube_transcript_api import (
    AgeRestricted,
    CouldNotRetrieveTranscript,
    IpBlocked,
    NoTranscriptFound,
    RequestBlocked,
    TranscriptsDisabled,
    VideoUnavailable,
    VideoUnplayable,
    YouTubeRequestFailed,
    YouTubeTranscriptApi,
)

from notes_common import chaos, ratelimit
from notes_common.errors import PermanentError, RateLimited, TransientError
from notes_common.redis_client import get_redis

log = logging.getLogger(__name__)

STREAM_FORMAT = "bv*[height<=720][ext=mp4]/bv*[height<=720]/b[height<=720]/bv*/b"


class _QuietLogger:
    def debug(self, msg):  # noqa: D401
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        log.debug("yt-dlp: %s", msg)

    def error(self, msg):
        log.debug("yt-dlp: %s", msg)


def _guard() -> None:
    chaos.maybe_429("youtube")
    ratelimit.acquire("youtube", 1, api="youtube")


def _classify_ytdlp(exc: Exception) -> Exception:
    msg = str(exc)
    low = msg.lower()
    if "429" in low or "too many requests" in low:
        return RateLimited("youtube")
    if "private video" in low:
        return TransientError("This video is private or unavailable", "VIDEO_UNAVAILABLE")
    if "video unavailable" in low or "not available" in low or "removed" in low or "does not exist" in low:
        return TransientError("This video is unavailable", "VIDEO_UNAVAILABLE")
    if "confirm your age" in low or "age-restricted" in low:
        return PermanentError("Age-restricted videos are not supported", "VIDEO_AGE_RESTRICTED")
    if "members-only" in low or "join this channel" in low:
        return PermanentError("Members-only videos are not supported", "VIDEO_MEMBERS_ONLY")
    if "not a bot" in low or "sign in" in low:
        return TransientError("YouTube asked for a sign-in check - retrying", "YOUTUBE_BOT_CHECK")
    return TransientError(f"Could not read the video from YouTube: {msg[:200]}", "YOUTUBE_ERROR")


def _ydl(opts: dict) -> yt_dlp.YoutubeDL:
    base = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True, "logger": _QuietLogger()}
    base.update(opts)
    return yt_dlp.YoutubeDL(base)


def fetch_metadata(video_id: str) -> dict:
    _guard()
    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        with _ydl({"format": STREAM_FORMAT}) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as exc:
        raise _classify_ytdlp(exc) from exc
    except Exception as exc:  # network errors etc.
        raise TransientError(f"Could not read the video from YouTube: {exc}", "YOUTUBE_ERROR") from exc

    if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming"):
        raise PermanentError("Live streams and premieres are not supported", "VIDEO_IS_LIVE")
    stream = info.get("url") or next((f.get("url") for f in info.get("requested_formats") or [] if f.get("url")), None)
    if stream:
        get_redis().set(f"stream:{video_id}", stream, ex=3 * 3600)
    return {
        "title": info.get("title") or video_id,
        "channel": info.get("channel") or info.get("uploader") or "",
        "duration_sec": float(info.get("duration") or 0),
        "thumbnail": info.get("thumbnail"),
        "upload_date": info.get("upload_date"),
    }


def stream_url(video_id: str, refresh: bool = False) -> str:
    r = get_redis()
    key = f"stream:{video_id}"
    if not refresh:
        cached = r.get(key)
        if cached:
            return cached
    _guard()
    try:
        with _ydl({"format": STREAM_FORMAT}) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
    except yt_dlp.utils.DownloadError as exc:
        raise _classify_ytdlp(exc) from exc
    url = info.get("url") or next((f.get("url") for f in info.get("requested_formats") or [] if f.get("url")), None)
    if not url:
        raise TransientError("No playable video stream found", "NO_STREAM")
    r.set(key, url, ex=3 * 3600)
    return url


def fetch_transcript(video_id: str) -> tuple[list[dict], str]:
    """Return ([{start, duration, text}], language_code). Prefers English, falls back to any
    available transcript (translated to English when YouTube allows it)."""
    _guard()
    api = YouTubeTranscriptApi()
    try:
        try:
            fetched = api.fetch(video_id, languages=("en", "en-US", "en-GB", "en-IN"))
        except NoTranscriptFound:
            listing = api.list(video_id)
            transcript = next(iter(listing))
            if transcript.is_translatable and not transcript.language_code.startswith("en"):
                try:
                    transcript = transcript.translate("en")
                except Exception:  # translation unavailable - use the original language
                    pass
            fetched = transcript.fetch()
    except (TranscriptsDisabled, NoTranscriptFound, StopIteration) as exc:
        raise PermanentError("This video has no captions/transcript available", "NO_TRANSCRIPT") from exc
    except AgeRestricted as exc:
        raise PermanentError("Age-restricted videos are not supported", "VIDEO_AGE_RESTRICTED") from exc
    except (VideoUnavailable, VideoUnplayable) as exc:
        raise TransientError("This video is unavailable", "VIDEO_UNAVAILABLE") from exc
    except (RequestBlocked, IpBlocked) as exc:
        raise RateLimited("youtube") from exc
    except YouTubeRequestFailed as exc:
        if "429" in str(exc):
            raise RateLimited("youtube") from exc
        raise TransientError("YouTube transcript request failed", "TRANSCRIPT_FETCH_FAILED") from exc
    except CouldNotRetrieveTranscript as exc:
        raise TransientError("Could not retrieve the transcript", "TRANSCRIPT_FETCH_FAILED") from exc
    except Exception as exc:
        raise TransientError(f"Could not retrieve the transcript: {exc}", "TRANSCRIPT_FETCH_FAILED") from exc

    segments = [
        {"start": round(float(s.start), 2), "duration": round(float(s.duration), 2), "text": s.text.replace("\n", " ").strip()}
        for s in fetched
        if s.text and s.text.strip()
    ]
    if not segments:
        raise PermanentError("This video's transcript is empty", "NO_TRANSCRIPT")
    return segments, getattr(fetched, "language_code", "en")
