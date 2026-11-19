"""Fault injection used to demonstrate failure handling live (spec section 6).

State lives in Redis so a single `POST /debug/chaos` on the gateway affects every worker:

    chaos:429:<api>     integer - the next N calls to <api> (llm | notion | youtube) behave as HTTP 429
    chaos:frame_fail    "all", "first:N", or comma-separated timestamps (seconds) whose capture must fail

Injected 429s raise exactly the same RateLimited error that a real provider 429 raises,
so the demo exercises the production retry path.
"""

from __future__ import annotations

from .errors import RateLimited
from .redis_client import get_redis

APIS = ("llm", "notion", "youtube")


def set_429(api: str, count: int) -> None:
    get_redis().set(f"chaos:429:{api}", int(count))


def set_frame_fail(value: str | None) -> None:
    r = get_redis()
    if value:
        r.set("chaos:frame_fail", value)
    else:
        r.delete("chaos:frame_fail")


def state() -> dict:
    r = get_redis()
    return {
        **{f"{api}_429": int(r.get(f"chaos:429:{api}") or 0) for api in APIS},
        "frame_fail": r.get("chaos:frame_fail") or "",
    }


def clear() -> None:
    r = get_redis()
    r.delete(*[f"chaos:429:{api}" for api in APIS], "chaos:frame_fail")


def maybe_429(api: str) -> None:
    r = get_redis()
    key = f"chaos:429:{api}"
    if not r.exists(key):
        return
    remaining = r.decr(key)
    if remaining < 0:
        r.delete(key)
        return
    raise RateLimited(api, retry_after=None, simulated=True)


def frame_should_fail(timestamp_sec: float, idx: int = 0) -> bool:
    """`value` may be "all", "first:N" (fail the first N flagged frames) or a list of timestamps."""
    value = get_redis().get("chaos:frame_fail")
    if not value:
        return False
    value = value.strip().lower()
    if value == "all":
        return True
    if value.startswith("first:"):
        try:
            return idx < int(value.split(":", 1)[1])
        except ValueError:
            return False
    targets = []
    for part in value.split(","):
        try:
            targets.append(float(part))
        except ValueError:
            continue
    return any(abs(t - timestamp_sec) < 1.5 for t in targets)
