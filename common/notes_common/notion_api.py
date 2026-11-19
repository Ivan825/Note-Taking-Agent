"""Minimal Notion REST client with global rate limiting and error classification."""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Any

import httpx

from . import chaos, ratelimit
from .config import get_settings
from .errors import PermanentError, RateLimited, TransientError
from .redis_client import get_redis

log = logging.getLogger(__name__)

API = "https://api.notion.com/v1"
NOTION_VERSION = "2026-03-11"


def normalize_id(value: str) -> str:
    """Accept a raw id, a dashed uuid, or a full notion.so URL and return a dashed uuid."""
    value = (value or "").strip()
    m = re.findall(r"[0-9a-fA-F]{32}|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", value)
    if not m:
        raise PermanentError(f"'{value}' is not a valid Notion page or database id", "INVALID_NOTION_PARENT")
    raw = m[-1].replace("-", "").lower()
    return f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:]}"


class NotionClient:
    def __init__(self, token: str, timeout: float = 30.0):
        self.token = token
        self._http = httpx.Client(
            base_url=API,
            timeout=timeout,
            headers={"Authorization": f"Bearer {token}", "Notion-Version": NOTION_VERSION},
        )

    def close(self) -> None:
        self._http.close()

    # ------------------------------------------------------------------ core
    def request(self, method: str, path: str, *, json: Any = None, files: Any = None, ok_404: bool = False) -> dict | None:
        chaos.maybe_429("notion")
        ratelimit.acquire("notion", 1, api="notion")
        try:
            resp = self._http.request(method, path, json=json, files=files)
        except httpx.HTTPError as exc:
            raise TransientError(f"Could not reach Notion: {exc}", "NOTION_UNREACHABLE") from exc

        if resp.status_code < 300:
            return resp.json()
        if resp.status_code == 404 and ok_404:
            return None

        try:
            body = resp.json()
        except ValueError:
            body = {"message": resp.text[:300]}
        msg = body.get("message", "")
        code = body.get("code", "")
        if resp.status_code == 429:
            ra = resp.headers.get("Retry-After")
            raise RateLimited("notion", retry_after=float(ra) if ra else None)
        if resp.status_code in (500, 502, 503, 504) or code in ("conflict_error", "service_unavailable"):
            raise TransientError(f"Notion temporarily unavailable ({resp.status_code}): {msg}", "NOTION_UNAVAILABLE")
        if resp.status_code == 401:
            raise PermanentError("Notion rejected the access token - reconnect Notion in the extension", "NOTION_UNAUTHORIZED")
        if resp.status_code in (403, 404):
            raise PermanentError(
                "The Notion page was not found or is not shared with the Video Note Taker connection",
                "NOTION_PARENT_NOT_ACCESSIBLE",
            )
        raise PermanentError(f"Notion rejected the request: {msg}", "NOTION_BAD_REQUEST")

    # ------------------------------------------------------------------ helpers
    def create_page(self, body: dict) -> dict:
        return self.request("POST", "/pages", json=body)  # type: ignore[return-value]

    def list_children(self, block_id: str, max_pages: int = 20) -> list[dict]:
        out: list[dict] = []
        cursor = None
        for _ in range(max_pages):
            q = f"/blocks/{block_id}/children?page_size=100" + (f"&start_cursor={cursor}" if cursor else "")
            data = self.request("GET", q) or {}
            out.extend(data.get("results", []))
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
        return out

    def search_pages(self, query: str) -> list[dict]:
        data = self.request(
            "POST",
            "/search",
            json={"query": query, "filter": {"property": "object", "value": "page"}, "page_size": 50},
        ) or {}
        return data.get("results", [])

    def upload_image(self, filename: str, data: bytes, content_type: str = "image/jpeg") -> str:
        created = self.request("POST", "/file_uploads", json={"filename": filename, "content_type": content_type})
        upload_id = created["id"]  # type: ignore[index]
        sent = self.request("POST", f"/file_uploads/{upload_id}/send", files={"file": (filename, data, content_type)})
        if (sent or {}).get("status") != "uploaded":
            raise TransientError(f"Notion upload did not complete (status={sent and sent.get('status')})", "NOTION_UPLOAD_FAILED")
        return upload_id

    # ------------------------------------------------------------------ parents
    def resolve_parent(self, parent_id: str) -> dict:
        """Return a Notion `parent` object for a page id or a database id."""
        pid = normalize_id(parent_id)
        page = self.request("GET", f"/pages/{pid}", ok_404=True)
        if page is not None:
            return {"type": "page_id", "page_id": pid}
        db = self.request("GET", f"/databases/{pid}", ok_404=True)
        if db is not None:
            sources = db.get("data_sources") or []
            if not sources:
                raise PermanentError("The Notion database has no data source", "NOTION_PARENT_NOT_ACCESSIBLE")
            ds_id = sources[0]["id"]
            ds = self.request("GET", f"/data_sources/{ds_id}") or {}
            title_prop = next(
                (name for name, p in (ds.get("properties") or {}).items() if p.get("type") == "title"), "Name"
            )
            return {"type": "data_source_id", "data_source_id": ds_id, "_title_prop": title_prop}
        raise PermanentError(
            "The Notion page was not found or is not shared with the Video Note Taker connection",
            "NOTION_PARENT_NOT_ACCESSIBLE",
        )

    def find_child_page_with_tag(self, parent: dict, tag: str) -> dict | None:
        """Find a page already created for this job (idempotent publish after a crash)."""
        if parent["type"] == "page_id":
            for block in self.list_children(parent["page_id"]):
                if block.get("type") == "child_page" and tag in (block.get("child_page") or {}).get("title", ""):
                    if block.get("in_trash") or block.get("archived"):
                        continue
                    return {"id": block["id"]}
            return None
        data = self.request(
            "POST",
            f"/data_sources/{parent['data_source_id']}/query",
            json={"filter": {"property": parent["_title_prop"], "title": {"contains": tag}}, "page_size": 5},
        ) or {}
        results = [r for r in data.get("results", []) if not r.get("in_trash")]
        return results[0] if results else None

    def ensure_default_page(self, title: str) -> str | None:
        """Find or create the user's default destination page ("Video Notes").

        Public connections may create private pages at the workspace level. The id is cached
        in Redis (per token) so concurrent jobs never create two default pages.
        """
        r = get_redis()
        key = "notion:default:" + hashlib.sha256(self.token.encode()).hexdigest()[:32]
        cached = r.get(key)
        if cached:
            return cached
        lock = r.lock(key + ":lock", timeout=60, blocking_timeout=60)
        with lock:
            cached = r.get(key)
            if cached:
                return cached
            page_id = None
            for page in self.search_pages(title):
                if page.get("in_trash") or page.get("archived"):
                    continue
                if (page.get("parent") or {}).get("type") != "workspace":
                    continue
                if _page_title(page).strip() == title:
                    page_id = page["id"]
                    break
            if page_id is None:
                try:
                    created = self.create_page(
                        {
                            "parent": {"type": "workspace", "workspace": True},
                            "icon": {"type": "emoji", "emoji": "🎬"},
                            "properties": {"title": {"title": [{"type": "text", "text": {"content": title}}]}},
                            "children": [
                                _paragraph(
                                    "Study notes generated by the Video Note Taker extension appear here. "
                                    "Each note is a sub-page with a summary and captioned frames from the video."
                                )
                            ],
                        }
                    )
                    page_id = created["id"]
                except PermanentError as exc:
                    log.warning("could not create workspace-level page (%s); falling back to a shared page", exc)
                    shared = [p for p in self.search_pages("") if not p.get("in_trash")]
                    page_id = shared[0]["id"] if shared else None
            if page_id:
                r.set(key, page_id, ex=30 * 24 * 3600)
            return page_id


def _page_title(page: dict) -> str:
    for prop in (page.get("properties") or {}).values():
        if prop.get("type") == "title":
            return "".join(t.get("plain_text", "") for t in prop.get("title", []))
    return ""


def _paragraph(text: str) -> dict:
    return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": [{"type": "text", "text": {"content": text}}]}}


def exchange_oauth_code(code: str, redirect_uri: str | None) -> dict:
    s = get_settings()
    if not (s.notion_client_id and s.notion_client_secret):
        raise PermanentError("Notion OAuth is not configured on the server", "OAUTH_NOT_CONFIGURED")
    body: dict[str, Any] = {"grant_type": "authorization_code", "code": code}
    body["redirect_uri"] = redirect_uri or s.notion_redirect_uri
    try:
        resp = httpx.post(
            f"{API}/oauth/token",
            json=body,
            auth=(s.notion_client_id, s.notion_client_secret),
            headers={"Notion-Version": NOTION_VERSION},
            timeout=30,
        )
    except httpx.HTTPError as exc:
        raise TransientError(f"Could not reach Notion: {exc}", "NOTION_UNREACHABLE") from exc
    if resp.status_code != 200:
        try:
            detail = resp.json().get("error_description") or resp.json().get("message") or resp.text
        except ValueError:
            detail = resp.text
        raise PermanentError(f"Notion authorization failed: {detail}", "OAUTH_EXCHANGE_FAILED")
    return resp.json()
