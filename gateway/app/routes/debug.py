"""Fault-injection endpoints for the live failure-handling demo (ENABLE_DEBUG_ENDPOINTS=true)."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from notes_common import chaos

router = APIRouter(prefix="/debug", tags=["debug"])


class ChaosBody(BaseModel):
    llm_429: int | None = Field(None, ge=0, le=50, description="Next N Claude calls behave as HTTP 429")
    notion_429: int | None = Field(None, ge=0, le=50, description="Next N Notion calls behave as HTTP 429")
    youtube_429: int | None = Field(None, ge=0, le=50, description="Next N YouTube calls behave as HTTP 429")
    frame_fail: str | None = Field(None, description='"all", "first:N", or comma-separated timestamps in seconds')
    no_captions: bool | None = Field(None, description="Pretend YouTube has no captions (speech-to-text fallback)")


@router.get("/chaos")
def get_chaos():
    return chaos.state()


@router.post("/chaos")
def set_chaos(body: ChaosBody):
    for api in chaos.APIS:
        value = getattr(body, f"{api}_429")
        if value is not None:
            chaos.set_429(api, value)
    if body.frame_fail is not None:
        chaos.set_frame_fail(body.frame_fail)
    if body.no_captions is not None:
        chaos.set_no_captions(body.no_captions)
    return chaos.state()


@router.delete("/chaos")
def clear_chaos():
    chaos.clear()
    return chaos.state()
