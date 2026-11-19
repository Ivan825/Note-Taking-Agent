"""API Gateway: accepts jobs, exposes status/results. It never performs pipeline work."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from notes_common import db
from notes_common.broker import Publisher
from notes_common.config import get_settings
from notes_common.logging_setup import setup_logging

from . import errors
from .routes import debug, health, jobs, oauth


def create_app(publisher=None, init_infra: bool = True) -> FastAPI:
    setup_logging("gateway")
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if init_infra:
            db.init_db()
        app.state.publisher = publisher or Publisher()
        if init_infra:
            app.state.publisher.queue_stats()  # declares the topology on startup
        yield
        if hasattr(app.state.publisher, "close"):
            app.state.publisher.close()

    app = FastAPI(
        title="Video Note-Taking Agent - Gateway",
        version="1.0.0",
        description="Submit YouTube videos for asynchronous note-taking and track their progress.",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=["Location"],
    )
    errors.install(app)
    app.include_router(health.router)
    app.include_router(jobs.router)
    app.include_router(oauth.router)
    if settings.enable_debug_endpoints:
        app.include_router(debug.router)

    @app.get("/", include_in_schema=False)
    def root():
        return {"service": "video-note-taker-gateway", "docs": "/docs", "health": "/healthz"}

    return app


app = create_app()
