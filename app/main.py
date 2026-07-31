"""FastAPI entry point for the Arabic stock opportunity platform."""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import routes_cost, routes_dashboard, routes_debug, routes_lock, routes_opportunities, routes_prices, routes_spx, routes_web
from app.config import get_settings
from app.db.session import (
    database_backend,
    database_is_ephemeral,
    init_db,
    release_interrupted_runs,
)
from app.live.prices import start_price_stream, stop_price_stream
from app.opportunities.audit_scheduler import start_audit_scheduler, stop_audit_scheduler
from app.providers.factory import get_market_data_provider
from app.security.site_lock import SiteLockMiddleware

logging.basicConfig(level=logging.INFO, format="%(levelname)s [%(name)s] %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    settings = get_settings()
    interrupted = release_interrupted_runs()
    if interrupted:
        logging.getLogger(__name__).info("released %d interrupted scan run(s)", interrupted)
    if database_is_ephemeral():
        logging.getLogger(__name__).warning(
            "DATABASE_URL is unset: running on SQLite, so every saved scan is lost "
            "on the next deploy or restart"
        )
    started = settings.enable_self_audit_scheduler and not os.environ.get("PYTEST_CURRENT_TEST")
    if started:
        start_audit_scheduler()
    if settings.finnhub_api_key and not os.environ.get("PYTEST_CURRENT_TEST"):
        # Warm the saved earnings calendar without delaying application startup
        # or any web response. The service itself serves cache/stale fallback.
        routes_dashboard._executor.submit(routes_dashboard._refresh_earnings)
        routes_dashboard._news_executor.submit(routes_dashboard._refresh_news)
    if settings.spx_enabled and settings.alpaca_api_key and not os.environ.get("PYTEST_CURRENT_TEST"):
        routes_spx._executor.submit(routes_spx._refresh, routes_spx.StrikeMode.NEAR)
    streaming = not os.environ.get("PYTEST_CURRENT_TEST") and start_price_stream(settings)
    yield
    if streaming:
        stop_price_stream()
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
app.include_router(routes_prices.router)
app.include_router(routes_spx.router)
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
        "stock_feed": settings.alpaca_feed if settings.market_data_provider == "alpaca" else settings.market_data_provider,
        "stock_overnight_feed": (
            settings.alpaca_overnight_feed
            if settings.market_data_provider == "alpaca" else None
        ),
        "options_enabled": settings.options_enabled,
        "options_feed": settings.alpaca_options_feed if settings.options_enabled else "disabled",
        "paper_trading_only": True,
        "provider_healthy": provider_healthy,
        "database": database_backend(),
        "results_survive_restart": not database_is_ephemeral(),
        "ai_provider": "openai",
        "openai_configured": bool(settings.openai_api_key),
        "deployed_commit_sha": os.environ.get("RENDER_GIT_COMMIT"),
    }
