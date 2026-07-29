"""FastAPI entry point for the Arabic stock opportunity platform."""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import routes_cost, routes_dashboard, routes_debug, routes_lock, routes_opportunities, routes_web
from app.config import get_settings
from app.db.session import init_db
from app.opportunities.audit_scheduler import start_audit_scheduler, stop_audit_scheduler
from app.providers.factory import get_market_data_provider
from app.security.site_lock import SiteLockMiddleware

logging.basicConfig(level=logging.INFO, format="%(levelname)s [%(name)s] %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    settings = get_settings()
    started = settings.enable_self_audit_scheduler and not os.environ.get("PYTEST_CURRENT_TEST")
    if started:
        start_audit_scheduler()
    yield
    if started:
        stop_audit_scheduler()


app = FastAPI(
    title="منصة تحليل الأسهم الأمريكية",
    description="تحليل مشروط للأسهم الأمريكية، دون تنفيذ تداول آلي أو ضمان للربح.",
    version="2.0.0",
    lifespan=lifespan,
)
app.add_middleware(SiteLockMiddleware)
app.include_router(routes_lock.router)
app.include_router(routes_opportunities.router)
app.include_router(routes_cost.router)
app.include_router(routes_debug.router)
app.include_router(routes_dashboard.router)
app.include_router(routes_web.router)


@app.get("/api/v1/health")
def health() -> dict:
    settings = get_settings()
    try:
        provider_status = get_market_data_provider().provider_name
        provider_healthy = True
    except Exception as exc:
        provider_status = f"error:{type(exc).__name__}"
        provider_healthy = False
    return {
        "status": "ok",
        "env": settings.app_env,
        "market_data_provider": provider_status,
        "provider_healthy": provider_healthy,
        "ai_provider": "openai",
        "openai_configured": bool(settings.openai_api_key),
        "deployed_commit_sha": os.environ.get("RENDER_GIT_COMMIT"),
    }
