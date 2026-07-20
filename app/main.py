"""FastAPI entry point (SRS 3.1, 5.2)."""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

# uvicorn only configures its own loggers; without this, `app.*` INFO/WARNING messages
# (provider-failure reasons, scheduler start, self-audit summaries) never reach the console.
logging.basicConfig(
    level=logging.INFO, format="%(levelname)s [%(name)s] %(message)s"
)

from app.api import routes_analysis, routes_audit, routes_cost, routes_lock, routes_options, routes_screener, routes_web
from app.config import get_settings
from app.core.scheduler import start_scheduler, stop_scheduler
from app.providers.factory import get_market_data_provider
from app.security.site_lock import SiteLockMiddleware


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Skip during pytest (PYTEST_CURRENT_TEST is set automatically) so the test suite
    # never spins up a real background scheduler thread.
    settings = get_settings()
    started = False
    if settings.enable_self_audit_scheduler and not os.environ.get("PYTEST_CURRENT_TEST"):
        start_scheduler()
        started = True
    yield
    if started:
        stop_scheduler()


app = FastAPI(
    title="US Stock & Options Decision Intelligence Platform", version="0.1.0", lifespan=lifespan
)

# Site-wide access lock (Phase 5, owner request 2026-07-20) — inert unless
# ACCESS_CODE_MAIN is set in the environment (see app/security/site_lock.py).
app.add_middleware(SiteLockMiddleware)

app.include_router(routes_lock.router)
app.include_router(routes_analysis.router)
app.include_router(routes_audit.router)
app.include_router(routes_cost.router)
app.include_router(routes_options.router)
app.include_router(routes_screener.router)
app.include_router(routes_web.router)


@app.get("/api/v1/health")
def health() -> dict:
    settings = get_settings()
    provider_status = "unknown"
    try:
        provider = get_market_data_provider()
        provider_status = provider.provider_name
    except Exception as exc:  # provider misconfigured (e.g. missing API key)
        provider_status = f"error: {exc}"
    return {
        "status": "ok",
        "env": settings.app_env,
        "market_data_provider": provider_status,
        "llm_provider": settings.llm_provider,
    }
