import uuid

from notes_common.broker import pick_delay
from notes_common.config import get_settings
from notes_common.errors import RateLimited, TransientError
from notes_common.states import Status
from worker.pipeline import _retry_or_dead


def test_backoff_schedule_is_exponential_and_bounded():
    s = get_settings()
    assert [s.backoff_for(a) for a in range(1, 7)] == [10, 30, 90, 90, 90, 90]


def test_pick_delay_rounds_up_to_existing_queue():
    assert pick_delay(5) == 10
    assert pick_delay(10) == 10
    assert pick_delay(31) == 90
    assert pick_delay(1000) == 90


def test_rate_limit_is_retried_not_failed():
    out = _retry_or_dead(uuid.uuid4(), Status.SUMMARIZING, attempt=1, exc=RateLimited("llm"))
    assert out.kind == "retry"
    assert out.delay == 10
    assert out.next_attempt == 2


def test_retry_after_header_is_honoured():
    out = _retry_or_dead(uuid.uuid4(), Status.PUBLISHING, attempt=1, exc=RateLimited("notion", retry_after=25))
    assert out.kind == "retry"
    assert out.delay == 30


def test_budget_exhaustion_dead_letters():
    out = _retry_or_dead(uuid.uuid4(), Status.TRANSCRIBING, attempt=5,
                         exc=TransientError("This video is unavailable", "VIDEO_UNAVAILABLE"))
    assert out.kind == "dead"
    assert out.error["code"] == "VIDEO_UNAVAILABLE"
    assert out.error["attempts"] == 5
