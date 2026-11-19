"""Unit-test setup: SQLite instead of Postgres, a fake broker publisher, no network."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for sub in ("common", "gateway", "worker"):
    sys.path.insert(0, str(ROOT / sub))

_db = Path(tempfile.mkdtemp()) / "test.db"
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_db}")
from cryptography.fernet import Fernet  # noqa: E402

os.environ.setdefault("TOKEN_ENC_KEY", Fernet.generate_key().decode())
os.environ.setdefault("BACKOFF_SECONDS", "10,30,90,90")
os.environ.setdefault("MAX_ATTEMPTS", "5")
os.environ.setdefault("ENABLE_DEBUG_ENDPOINTS", "false")

import pytest  # noqa: E402


class FakePublisher:
    def __init__(self) -> None:
        self.published: list[dict] = []
        self.fail = False

    def publish_job(self, body: dict) -> None:
        if self.fail:
            raise RuntimeError("broker down")
        self.published.append(body)

    def queue_stats(self) -> dict:
        return {"work_ready": len(self.published), "consumers": 0, "dead_lettered": 0}

    def close(self) -> None:
        pass


@pytest.fixture
def publisher() -> FakePublisher:
    return FakePublisher()


@pytest.fixture
def client(publisher):
    from fastapi.testclient import TestClient

    from notes_common import db

    db.init_db()
    from app.main import create_app

    app = create_app(publisher=publisher, init_infra=False)
    with TestClient(app) as c:
        yield c
