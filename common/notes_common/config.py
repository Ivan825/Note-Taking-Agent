"""Typed settings loaded from environment variables (see .env.example)."""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    # Infrastructure
    database_url: str = "postgresql+psycopg://notes:notes@postgres:5432/notes"
    rabbitmq_url: str = "amqp://notes:notes@rabbitmq:5672/%2F"
    redis_url: str = "redis://redis:6379/0"
    public_base_url: str = "http://localhost:8000"

    # LLM
    anthropic_api_key: str = ""
    llm_model: str = "claude-haiku-5-5"
    llm_effort: str = "medium"
    llm_rpm: int = 40
    llm_itpm: int = 40_000
    llm_chunk_tokens: int = 60_000
    llm_max_frames: int = 8
    frame_vision_check: bool = True

    # Notion
    notion_client_id: str = ""
    notion_client_secret: str = ""
    notion_redirect_uri: str = ""
    notion_rps: float = 2.5
    notion_version: str = "2022-06-28"
    default_notion_page_title: str = "Video Notes"

    # YouTube
    youtube_rps: float = 1.0
    youtube_cookies_file: str = ""

    # Speech-to-text fallback (videos without captions)
    asr_fallback: bool = True
    whisper_model: str = "base"
    whisper_threads: int = 4

    # Retry policy
    max_attempts: int = 5
    backoff_seconds: Annotated[list[int], NoDecode] = [10, 30, 90, 90]

    # Limits
    max_video_seconds: int = 1200
    frame_timeout_seconds: int = 60

    # Security / demo
    token_enc_key: str = ""
    enable_debug_endpoints: bool = False
    log_level: str = "INFO"

    @field_validator("backoff_seconds", mode="before")
    @classmethod
    def _parse_backoff(cls, v):
        if isinstance(v, str):
            parts = [p.strip() for p in v.split(",") if p.strip()]
            return [int(p) for p in parts] or [10]
        return v

    def backoff_for(self, attempt: int) -> int:
        """Delay before retry number `attempt` (1-based). The last value repeats."""
        steps = self.backoff_seconds or [10]
        return steps[min(max(attempt, 1), len(steps)) - 1]


@lru_cache
def get_settings() -> Settings:
    return Settings()
