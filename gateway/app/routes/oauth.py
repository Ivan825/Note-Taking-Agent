"""Notion OAuth helper endpoints.

The client secret must never ship inside the browser extension, so the extension sends the
authorization code here and the gateway exchanges it. The resulting token is returned to the
extension and NOT stored server-side.
"""

from __future__ import annotations

import logging
import secrets
from urllib.parse import urlencode

from fastapi import APIRouter
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from notes_common.config import get_settings
from notes_common.errors import PipelineError
from notes_common.notion_api import NotionClient, exchange_oauth_code

from ..errors import ApiError

log = logging.getLogger(__name__)
router = APIRouter(prefix="/oauth/notion", tags=["oauth"])


class ExchangeBody(BaseModel):
    code: str
    redirect_uri: str | None = None


@router.get("/config")
def oauth_config():
    s = get_settings()
    if not s.notion_client_id:
        raise ApiError(503, "OAUTH_NOT_CONFIGURED", "NOTION_CLIENT_ID is not set on the server")
    state = secrets.token_urlsafe(16)
    params = {
        "client_id": s.notion_client_id,
        "response_type": "code",
        "owner": "user",
        "redirect_uri": s.notion_redirect_uri,
        "state": state,
    }
    return {
        "authorize_url": "https://api.notion.com/v1/oauth/authorize?" + urlencode(params),
        "redirect_uri": s.notion_redirect_uri,
        "state": state,
    }


def _exchange(code: str, redirect_uri: str | None) -> dict:
    data = exchange_oauth_code(code, redirect_uri)
    token = data["access_token"]
    default_parent = None
    client = NotionClient(token)
    try:
        default_parent = data.get("duplicated_template_id") or client.ensure_default_page(
            get_settings().default_notion_page_title
        )
    except PipelineError as exc:
        log.warning("could not prepare default Notion page: %s", exc)
    finally:
        client.close()
    return {
        "access_token": token,
        "refresh_token": data.get("refresh_token"),
        "expires_in": data.get("expires_in"),
        "bot_id": data.get("bot_id"),
        "workspace_id": data.get("workspace_id"),
        "workspace_name": data.get("workspace_name"),
        "workspace_icon": data.get("workspace_icon"),
        "default_parent_id": default_parent,
    }


@router.post("/exchange")
async def exchange(body: ExchangeBody):
    try:
        return await run_in_threadpool(_exchange, body.code, body.redirect_uri)
    except PipelineError as exc:
        raise ApiError(400, exc.code, exc.message)
