"""Fast HTML shell. All external work is started through background-job APIs."""
from __future__ import annotations

from pathlib import Path

import re

from fastapi import APIRouter, HTTPException, Request
from fastapi import Depends
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.api.routes_opportunities import build_results_summary
from app.db.session import get_db
from app.static_data.us_symbols import autocomplete_payload

router = APIRouter()
STATIC_DIR = Path(__file__).resolve().parents[1] / "static"
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / "templates"))


@router.get("/manifest.webmanifest", include_in_schema=False)
def manifest() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "manifest.webmanifest", media_type="application/manifest+json"
    )


@router.get("/sw.js", include_in_schema=False)
def service_worker() -> FileResponse:
    # Served from the root, not /static: a worker's scope cannot rise above the
    # path it was fetched from, and this one has to control the whole app.
    return FileResponse(
        STATIC_DIR / "sw.js",
        media_type="text/javascript",
        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"},
    )


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"symbol_catalog": autocomplete_payload()},
    )


@router.get("/earnings", response_class=HTMLResponse)
def earnings_dashboard(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="earnings.html",
        context={},
    )


@router.get("/news", response_class=HTMLResponse)
def news_dashboard(request: Request):
    return templates.TemplateResponse(request=request, name="news.html", context={})


@router.get("/spx", response_class=HTMLResponse)
def spx_dashboard(request: Request):
    return templates.TemplateResponse(request=request, name="spx.html", context={})


@router.get("/results", response_class=HTMLResponse)
def results_dashboard(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        request=request,
        name="results.html",
        context={"summary": build_results_summary(db)},
    )


@router.get("/trading-room", response_class=HTMLResponse)
def trading_room(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="trading_room.html",
        context={"symbol_catalog": autocomplete_payload()},
    )


@router.get("/stocks/{symbol}", response_class=HTMLResponse)
def stock_dashboard(request: Request, symbol: str):
    symbol = symbol.upper().strip()
    if not re.fullmatch(r"[A-Z][A-Z0-9.-]{0,9}", symbol):
        raise HTTPException(422, "رمز السهم غير صالح")
    return templates.TemplateResponse(
        request=request,
        name="stock.html",
        context={"symbol": symbol},
    )
