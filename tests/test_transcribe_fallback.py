"""Videos without captions fall back to speech-to-text; other errors are not masked."""

import uuid

import pytest

from notes_common.db import init_db, session_scope
from notes_common.errors import PermanentError
from notes_common.models import Job
from worker import jobstore
from worker.stages import transcribe

META = {"title": "No captions here", "channel": "x", "duration_sec": 120.0, "thumbnail": None, "upload_date": None}
ASR_SEGMENTS = [{"start": 0.0, "duration": 4.0, "text": "hello from speech recognition"}]


@pytest.fixture
def job_id():
    init_db()
    jid = uuid.uuid4()
    with session_scope() as db:
        db.add(Job(id=jid, video_id="C0DPdy98e4c", youtube_url="https://www.youtube.com/watch?v=C0DPdy98e4c",
                   status="TRANSCRIBING", warnings=[]))
    return jid


def _no_captions(_video_id):
    raise PermanentError("no captions", "NO_TRANSCRIPT")


def test_falls_back_to_speech_to_text(job_id, monkeypatch):
    monkeypatch.setattr(transcribe.youtube, "fetch_metadata", lambda v: META)
    monkeypatch.setattr(transcribe.youtube, "fetch_transcript", _no_captions)
    monkeypatch.setattr(transcribe, "_speech_to_text", lambda j, v, d: (ASR_SEGMENTS, "en"))

    transcribe.run(job_id, "C0DPdy98e4c")

    t = jobstore.get_artifact(job_id, "transcript")
    assert t["source"] == "speech_to_text"
    assert t["segments"] == ASR_SEGMENTS


def test_captions_used_when_available(job_id, monkeypatch):
    monkeypatch.setattr(transcribe.youtube, "fetch_metadata", lambda v: META)
    captions = [{"start": 1, "duration": 2, "text": "cc"}]
    monkeypatch.setattr(transcribe.youtube, "fetch_transcript", lambda v: (captions, "en"))
    monkeypatch.setattr(transcribe, "_speech_to_text", lambda *a: pytest.fail("ASR should not run"))

    transcribe.run(job_id, "C0DPdy98e4c")
    assert jobstore.get_artifact(job_id, "transcript")["source"] == "captions"


def test_other_permanent_errors_are_not_masked(job_id, monkeypatch):
    def age_restricted(_v):
        raise PermanentError("age restricted", "VIDEO_AGE_RESTRICTED")

    monkeypatch.setattr(transcribe.youtube, "fetch_metadata", lambda v: META)
    monkeypatch.setattr(transcribe.youtube, "fetch_transcript", age_restricted)
    with pytest.raises(PermanentError) as exc:
        transcribe.run(job_id, "C0DPdy98e4c")
    assert exc.value.code == "VIDEO_AGE_RESTRICTED"


def test_too_long_rejected_before_any_transcription(job_id, monkeypatch):
    monkeypatch.setattr(transcribe.youtube, "fetch_metadata", lambda v: {**META, "duration_sec": 1201.0})
    monkeypatch.setattr(transcribe.youtube, "fetch_transcript", lambda v: pytest.fail("must not fetch"))
    with pytest.raises(PermanentError) as exc:
        transcribe.run(job_id, "C0DPdy98e4c")
    assert exc.value.code == "VIDEO_TOO_LONG"
