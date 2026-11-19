"""Database engine/session helpers."""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from functools import lru_cache
from typing import Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .config import get_settings
from .models import Base

log = logging.getLogger(__name__)


@lru_cache
def get_engine() -> Engine:
    url = get_settings().database_url
    kwargs: dict = {"pool_pre_ping": True}
    if url.startswith("sqlite"):
        kwargs = {"connect_args": {"check_same_thread": False}}
    return create_engine(url, **kwargs)


@lru_cache
def _session_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), expire_on_commit=False)


@contextmanager
def session_scope() -> Iterator[Session]:
    session = _session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db(retries: int = 30) -> None:
    """Create tables (idempotent). Retries while Postgres is still starting."""
    for attempt in range(1, retries + 1):
        try:
            engine = get_engine()
            Base.metadata.create_all(engine)
            if engine.dialect.name == "postgresql":  # create_all never alters an existing table
                with engine.begin() as conn:
                    conn.execute(text("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS stage_timings JSON"))
            return
        except Exception as exc:  # pragma: no cover - startup race
            log.warning("database not ready (%s/%s): %s", attempt, retries, exc)
            time.sleep(1)
    raise RuntimeError("database unavailable")


def ping() -> None:
    with get_engine().connect() as conn:
        conn.execute(text("SELECT 1"))
