"""Global rate limiting shared by every worker process (spec section 10: "how the global
LLM rate limit is coordinated across workers").

Each external API gets a token bucket stored in Redis and updated atomically by a Lua
script, using Redis' own clock so worker clock skew does not matter. A worker must take
tokens before calling the API. Budgets are configured *below* the provider's published
limits, so a burst of jobs drains through the queue without the provider ever returning 429.

Short waits are absorbed by sleeping; if the wait would be long, the job is re-queued
through a delay queue instead of holding a worker hostage.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from .config import get_settings
from .errors import RateLimited
from .redis_client import get_redis

log = logging.getLogger(__name__)

WAIT_KEY_PREFIX = "metrics:ratelimit_wait:"

# KEYS[1] = bucket key
# ARGV    = capacity, refill_per_second, cost
# returns = seconds to wait (0 when the tokens were taken)
_LUA = """
local cap  = tonumber(ARGV[1])
local rate = tonumber(ARGV[2])
local cost = tonumber(ARGV[3])
local t    = redis.call('TIME')
local now  = tonumber(t[1]) + tonumber(t[2]) / 1000000
local b    = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(b[1])
local ts     = tonumber(b[2])
if tokens == nil then tokens = cap end
if ts == nil then ts = now end
tokens = math.min(cap, tokens + math.max(0, now - ts) * rate)
local wait = 0
if tokens >= cost then
  tokens = tokens - cost
else
  wait = (cost - tokens) / rate
end
redis.call('HSET', KEYS[1], 'tokens', tokens, 'ts', now)
redis.call('EXPIRE', KEYS[1], 3600)
return tostring(wait)
"""


@dataclass(frozen=True)
class Bucket:
    name: str
    capacity: float
    per_second: float


def buckets() -> dict[str, Bucket]:
    s = get_settings()
    rpm = max(1, s.llm_rpm)
    itpm = max(1000, s.llm_itpm)
    return {
        # Small burst capacity: providers enforce limits over short windows too.
        "llm_requests": Bucket("llm_requests", capacity=max(1.0, rpm / 6), per_second=rpm / 60),
        "llm_input_tokens": Bucket("llm_input_tokens", capacity=itpm / 4, per_second=itpm / 60),
        "notion": Bucket("notion", capacity=max(1.0, s.notion_rps), per_second=s.notion_rps),
        "youtube": Bucket("youtube", capacity=max(1.0, s.youtube_rps * 2), per_second=s.youtube_rps),
    }


_script = None


def _take(bucket: Bucket, cost: float) -> float:
    global _script
    r = get_redis()
    if _script is None:
        _script = r.register_script(_LUA)
    cost = min(cost, bucket.capacity)  # a single oversize request must still be admissible
    return float(_script(keys=[f"ratelimit:{bucket.name}"], args=[bucket.capacity, bucket.per_second, cost]))


def acquire(bucket_name: str, cost: float = 1.0, api: str | None = None, max_wait: float = 20.0) -> float:
    """Block until `cost` tokens are available. Returns seconds spent waiting.

    Raises RateLimited (-> job re-queued with backoff) if the wait would exceed `max_wait`.
    """
    bucket = buckets()[bucket_name]
    waited = 0.0
    try:
        while True:
            wait = _take(bucket, cost)
            if wait <= 0:
                if waited > 0.5:
                    log.info("rate limiter %s: waited %.1fs", bucket_name, waited)
                return waited
            if waited + wait > max_wait:
                raise RateLimited(api or bucket_name.split("_")[0], retry_after=wait, throttled=True)
            sleep_for = min(wait, 2.0)
            time.sleep(sleep_for)
            waited += sleep_for
    finally:
        if waited > 0:  # exported by the gateway as ratelimit_wait_seconds_total{bucket}
            get_redis().incrbyfloat(f"{WAIT_KEY_PREFIX}{bucket_name}", waited)
