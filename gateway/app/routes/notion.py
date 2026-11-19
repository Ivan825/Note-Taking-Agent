"""Notion helpers for the extension's options page. The token is used for one call and never stored."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from notes_common.errors import PipelineError, RateLimited, TransientError
from notes_common.notion_api import NotionClient, _page_title

from ..errors import ApiError

router = APIRouter(prefix="/notion", tags=["notion"])


class SearchBody(BaseModel):
    notion_token: str = Field(..., min_length=1)
    query: str = Field("", max_length=200)


def _icon(page: dict) -> str | None:
    icon = page.get("icon") or {}
    return icon.get("emoji") or (icon.get(icon.get("type") or "") or {}).get("url")


@router.post("/pages/search")
def search_pages(body: SearchBody):
    """Proxy Notion's POST /v1/search (pages only) -> [{id, title, icon}]."""
    client = NotionClient(body.notion_token.strip())
    try:
        pages = client.search_pages(body.query)
    except RateLimited as exc:
        raise ApiError(429, exc.code, exc.message)
    except TransientError as exc:
        raise ApiError(502, exc.code, exc.message)
    except PipelineError as exc:
        raise ApiError(401 if exc.code == "NOTION_UNAUTHORIZED" else 400, exc.code, exc.message)
    finally:
        client.close()
    return [
        {"id": p["id"], "title": _page_title(p) or "Untitled", "icon": _icon(p)}
        for p in pages
        if not (p.get("in_trash") or p.get("archived"))
    ]
