"""Persistent job state. Postgres is the source of truth for status and stage checkpoints."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, LargeBinary, String, Text, Uuid
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .states import Status


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    video_id: Mapped[str] = mapped_column(String(16), index=True)
    youtube_url: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default=Status.QUEUED.value, index=True)
    status_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    progress: Mapped[int] = mapped_column(Integer, default=0)

    video_title: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_sec: Mapped[float | None] = mapped_column(Float, nullable=True)

    # attempt number of the message currently allowed to work on this job (1-based)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    recoveries: Mapped[int] = mapped_column(Integer, default=0)
    worker_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    warnings: Mapped[list] = mapped_column(JSON, default=list)
    error: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # {"TRANSCRIBING": {"started_at": iso, "finished_at": iso}, ...} - first entry / completion per stage
    stage_timings: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    notion_parent_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    notion_page_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    notion_page_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Artifact(Base):
    """Stage checkpoint: output of a finished stage, keyed by (job, kind)."""

    __tablename__ = "job_artifacts"

    job_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), primary_key=True)  # meta | transcript | synthesis | chunk:N
    data: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Frame(Base):
    """One LLM-flagged timestamp and (if extraction succeeded) the real captured frame."""

    __tablename__ = "frames"

    job_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True)
    idx: Mapped[int] = mapped_column(Integer, primary_key=True)
    timestamp_sec: Mapped[float] = mapped_column(Float)
    captured_at_sec: Mapped[float | None] = mapped_column(Float, nullable=True)
    caption: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending | ok | failed
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    image: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    vision_checked: Mapped[int] = mapped_column(Integer, default=0)
    notion_upload_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    uploaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
