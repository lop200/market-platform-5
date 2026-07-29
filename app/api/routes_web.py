"""Fast HTML shell. All external work is started through background-job APIs."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi import Depends
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.api.routes_opportunities import build_results_summary
from app.db.session import get_db
from app.static_data.us_symbols import autocomplete_payload

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / "templates"))


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"symbol_catalog": autocomplete_payload()},
    )


@router.get("/results", response_class=HTMLResponse)
def results_dashboard(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        request=request,
        name="results.html",
        context={"summary": build_results_summary(db)},
    )
