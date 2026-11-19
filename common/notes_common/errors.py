"""Error taxonomy that drives the retry policy (spec section 6).

* RateLimited     - an external API answered 429 (or our own limiter is saturated): re-queue with backoff.
* TransientError  - may succeed later (network, 5xx, video temporarily unavailable): re-queue with backoff.
* PermanentError  - retrying cannot help (too long, no transcript, bad token): fail immediately.

Retries are bounded by MAX_ATTEMPTS; once exhausted the job is routed to the dead-letter queue.
"""

from __future__ import annotations


class PipelineError(Exception):
    code: str = "PIPELINE_ERROR"

    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message}


class RateLimited(PipelineError):
    code = "RATE_LIMITED"

    API_NAMES = {"llm": "Claude", "notion": "Notion", "youtube": "YouTube"}

    def __init__(self, api: str, retry_after: float | None = None, simulated: bool = False):
        name = self.API_NAMES.get(api, api)
        super().__init__(f"Rate-limited by {name}", code=f"{api.upper()}_RATE_LIMITED")
        self.api = api
        self.retry_after = retry_after
        self.simulated = simulated


class TransientError(PipelineError):
    code = "TRANSIENT_ERROR"


class PermanentError(PipelineError):
    code = "PERMANENT_ERROR"
