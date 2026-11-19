"""POST /notion/pages/search (Task 4)."""

from notes_common.errors import PermanentError
from notes_common.notion_api import NotionClient

PAGE = {
    "id": "p1",
    "icon": {"type": "emoji", "emoji": "🎬"},
    "properties": {"title": {"type": "title", "title": [{"plain_text": "Video Notes"}]}},
}


def test_search_returns_id_title_icon(client, monkeypatch):
    seen = {}

    def fake(self, query):
        seen["token"], seen["query"] = self.token, query
        return [PAGE, {**PAGE, "id": "gone", "in_trash": True}]

    monkeypatch.setattr(NotionClient, "search_pages", fake)
    r = client.post("/notion/pages/search", json={"notion_token": "tok", "query": "vid"})
    assert r.status_code == 200
    assert r.json() == [{"id": "p1", "title": "Video Notes", "icon": "🎬"}]
    assert seen == {"token": "tok", "query": "vid"}


def test_search_bad_token_is_401(client, monkeypatch):
    def fake(self, query):
        raise PermanentError("rejected", "NOTION_UNAUTHORIZED")

    monkeypatch.setattr(NotionClient, "search_pages", fake)
    r = client.post("/notion/pages/search", json={"notion_token": "tok", "query": ""})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "NOTION_UNAUTHORIZED"


def test_search_requires_token(client):
    assert client.post("/notion/pages/search", json={"query": "x"}).status_code == 400
