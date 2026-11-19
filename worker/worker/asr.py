"""Speech-to-text fallback for videos without captions.

When YouTube has no transcript, the worker downloads only the audio track and transcribes it
locally with Whisper (faster-whisper, CPU int8). The output has the same shape as YouTube
captions - [{start, duration, text}] - so the rest of the pipeline is unchanged.
"""

from __future__ import annotations

import logging
import subprocess
import threading
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:  # numpy comes with faster-whisper; imported lazily so unit tests stay light
    import numpy as np

from notes_common.config import get_settings
from notes_common.errors import PermanentError, TransientError

log = logging.getLogger(__name__)

MODEL_DIR = "/opt/whisper"
_model = None
_model_lock = threading.Lock()


def _get_model():
    global _model
    with _model_lock:
        if _model is None:
            from faster_whisper import WhisperModel  # heavy import - only when needed

            s = get_settings()
            log.info("loading Whisper model '%s'", s.whisper_model)
            _model = WhisperModel(s.whisper_model, device="cpu", compute_type="int8",
                                  download_root=MODEL_DIR, cpu_threads=s.whisper_threads)
        return _model


SAMPLE_RATE = 16000


def load_audio(path: str) -> "np.ndarray":
    """Decode any audio file to 16 kHz mono float32 with ffmpeg (no PyAV dependency)."""
    import numpy as np

    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", path,
           "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "-acodec", "pcm_s16le", "pipe:1"]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=600, check=True)
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.decode(errors="ignore")[-200:]
        raise TransientError(f"Could not decode the audio: {detail}", "ASR_FAILED") from exc
    except subprocess.TimeoutExpired as exc:
        raise TransientError("Decoding the audio timed out", "ASR_FAILED") from exc
    return np.frombuffer(proc.stdout, np.int16).astype(np.float32) / 32768.0


def transcribe_file(
    path: str, duration: float, on_progress: Callable[[float], None] | None = None
) -> tuple[list[dict], str]:
    audio = load_audio(path)
    if audio.size < SAMPLE_RATE // 2:
        raise PermanentError("This video has no captions and no recognizable speech", "NO_SPEECH")
    try:
        segments_iter, info = _get_model().transcribe(
            audio,
            beam_size=1,
            vad_filter=True,                  # skip music/silence
            condition_on_previous_text=False,  # avoids repetition loops on long audio
        )
        segments: list[dict] = []
        for seg in segments_iter:
            text = seg.text.strip()
            if text:
                segments.append({
                    "start": round(float(seg.start), 2),
                    "duration": round(float(seg.end - seg.start), 2),
                    "text": text,
                })
            if on_progress and duration:
                on_progress(min(1.0, seg.end / duration))
    except PermanentError:
        raise
    except Exception as exc:
        raise TransientError(f"Speech recognition failed: {exc}", "ASR_FAILED") from exc

    if not segments:
        raise PermanentError("This video has no captions and no recognizable speech", "NO_SPEECH")
    return segments, info.language or "unknown"
