"""GET /healthz - confirms the gateway, broker, database and Redis are reachable."""

from __future__ import annotations

import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from notes_common import db
from notes_common.config import get_settings
from notes_common.redis_client import get_redis

router = APIRouter(tags=["health"])


def _check(fn) -> dict:
    started = time.perf_counter()
    try:
        detail = fn()
        out = {"status": "ok", "latency_ms": round((time.perf_counter() - started) * 1000, 1)}
        if isinstance(detail, dict):
            out.update(detail)
        return out
    except Exception as exc:
        return {"status": "down", "error": str(exc)[:200]}


@router.get("/healthz")
def healthz(request: Request):
    s = get_settings()
    components = {
        "gateway": {"status": "ok"},
        "database": _check(db.ping),
        "broker": _check(request.app.state.publisher.queue_stats),
        "redis": _check(lambda: get_redis().ping()),
    }
    ok = all(c["status"] == "ok" for c in components.values())
    body = {
        "status": "ok" if ok else "degraded",
        "components": components,
        "config": {
            "llm_model": s.llm_model,
            "llm_rpm": s.llm_rpm,
            "max_attempts": s.max_attempts,
            "backoff_seconds": s.backoff_seconds,
            "max_video_seconds": s.max_video_seconds,
        },
    }
    return JSONResponse(status_code=200 if ok else 503, content=body)
