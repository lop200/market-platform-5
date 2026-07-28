"""Single-page RTL web UI (SRS section 6). Server-rendered (Jinja2, owner's choice) —
calls the orchestrator directly rather than round-tripping through the JSON API, so the
page needs no client-side JS or API-key handling for a single-owner personal deployment.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.core.cost_gate import CostGate
from app.core.orchestrator import (
    CostLimitExceededError,
    DataFetchError,
    InvalidSymbolError,
    VisionExtractionError,
    add_watchlist_item,
    get_live_quote,
    get_watchlist_item_events,
    list_watchlist_with_status,
    remove_watchlist_item,
    run_analysis_deterministic_only,
    run_analysis_narrative,
    run_option_image_analysis,
    run_screener_scan,
    run_snipe_options_scan,
    run_snipe_scan,
)
from app.db.session import get_db
from app.engines.screener.snipe_schemas import WatchlistAddRequest
from app.legal.disclaimers import DISCLAIMER_AR, DISCLAIMER_EN, SCREENER_DISCLAIMER_AR, SNIPE_DISCLAIMER_AR
from app.static_data.us_symbols import US_SYMBOLS

router = APIRouter(tags=["web"])
templates = Jinja2Templates(directory="app/templates")
templates.env.filters["chart_bars_json"] = lambda bars: json.dumps([b.model_dump() for b in bars])

MAX_IMAGE_BYTES = 8 * 1024 * 1024  # 8 MB
ALLOWED_MEDIA_TYPES = {"image/png", "image/jpeg", "image/webp"}
SYMBOLS_JSON = json.dumps(US_SYMBOLS, ensure_ascii=False)


def _base_context(db: Session, active_tab: str) -> dict:
    return {
        "cost_today": CostGate(db).spend_summary("today"),
        "result": None, "partial": None, "error": None, "symbol": None, "lang": "ar", "disclaimer": DISCLAIMER_AR,
        "option_result": None, "option_error": None, "active_tab": active_tab,
        "symbols_json": SYMBOLS_JSON,
        "screener_result": None, "screener_error": None, "screener_disclaimer": SCREENER_DISCLAIMER_AR,
        "snipe_result": None, "snipe_error": None, "snipe_disclaimer": SNIPE_DISCLAIMER_AR,
        "snipe_options_result": None, "snipe_options_error": None,
    }


@router.get("/", response_class=HTMLResponse)
def index(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    return templates.TemplateResponse(request, "index.html", _base_context(db, "stock"))


@router.post("/ui/analyze", response_class=HTMLResponse)
def ui_analyze(
    request: Request, symbol: str = Form(...), lang: str = Form("ar"), db: Session = Depends(get_db)
) -> HTMLResponse:
    """Phase 1 only (deterministic, new progressive-load flow — owner request
    2026-07-18): returns instantly with price/scores/plain-summary/chart on a cache miss,
    and the page's own JS fetches the LLM narrative afterward via
    `/ui/analyze/narrative/{analysis_id}` so the page itself never waits on the LLM call.
    A cache hit still renders everything at once (nothing was slow to begin with)."""
    result = None
    partial = None
    error = None
    try:
        result, partial = run_analysis_deterministic_only(db, symbol, lang=lang)
    except InvalidSymbolError as exc:
        error = f"رمز غير صالح: {exc}" if lang == "ar" else f"Invalid symbol: {exc}"
    except DataFetchError as exc:
        error = (
            f"تعذّر جلب بيانات لهذا الرمز — تأكد إنه رمز سهم أمريكي صحيح (مثل NVDA وليس اسم الشركة). {exc}"
            if lang == "ar" else f"Could not fetch data for this symbol — check it's a valid US ticker. {exc}"
        )
    except CostLimitExceededError as exc:
        error = exc.reason
    except Exception as exc:  # keep the page usable even on an unexpected failure
        error = f"تعذّر إكمال التحليل: {exc}" if lang == "ar" else f"Analysis failed: {exc}"

    context = _base_context(db, "stock")
    context.update(result=result, partial=partial, error=error, symbol=symbol, lang=lang,
                    disclaimer=DISCLAIMER_AR if lang == "ar" else DISCLAIMER_EN)
    return templates.TemplateResponse(request, "index.html", context)


@router.get("/ui/analyze/narrative/{analysis_id}")
def ui_analyze_narrative(analysis_id: str, lang: str = "ar", db: Session = Depends(get_db)) -> dict:
    """Phase 2 — fetched by the page's own JS right after a phase-1 (partial) render.
    Runs the LLM step only and returns the narrative fields as JSON for the page to patch
    into the DOM without a reload."""
    import uuid as uuid_module

    try:
        parsed_id = uuid_module.UUID(analysis_id)
    except ValueError:
        return {"error": "invalid analysis_id"}
    try:
        response = run_analysis_narrative(db, parsed_id, lang=lang)
    except CostLimitExceededError as exc:
        return {"error": exc.reason}
    except DataFetchError as exc:
        return {"error": f"تعذّر جلب بيانات إضافية لإكمال الشرح: {exc}"}
    except ValueError as exc:
        return {"error": str(exc)}
    except Exception as exc:
        return {"error": f"تعذّر توليد الشرح: {exc}"}
    return response.model_dump(mode="json")


@router.get("/ui/quote/{symbol}")
def ui_live_quote(symbol: str, db: Session = Depends(get_db)) -> dict:
    """Polled every 15s by the stock-analysis page while the market is open (new, owner
    request 2026-07-18) — kept separate from `/ui/analyze` so a price tick never re-runs
    the deterministic engine or the LLM, just a plain quote fetch."""
    try:
        return get_live_quote(db, symbol)
    except InvalidSymbolError as exc:
        return {"error": f"رمز غير صالح: {exc}"}
    except DataFetchError as exc:
        return {"error": f"تعذّر جلب السعر: {exc}"}
    except CostLimitExceededError as exc:
        return {"error": exc.reason}


@router.get("/ui/screener/daily", response_class=HTMLResponse)
def ui_screener_daily(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    context = _base_context(db, "screener-daily")
    try:
        context["screener_result"] = run_screener_scan(db, "daily_readiness")
    except CostLimitExceededError as exc:
        context["screener_error"] = exc.reason
    except Exception as exc:
        context["screener_error"] = f"تعذّر تشغيل الماسح الفني: {exc}"
    return templates.TemplateResponse(request, "index.html", context)


@router.get("/ui/screener/weekly", response_class=HTMLResponse)
def ui_screener_weekly(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    context = _base_context(db, "screener-weekly")
    try:
        context["screener_result"] = run_screener_scan(db, "weekly_structure")
    except CostLimitExceededError as exc:
        context["screener_error"] = exc.reason
    except Exception as exc:
        context["screener_error"] = f"تعذّر تشغيل الماسح الفني: {exc}"
    return templates.TemplateResponse(request, "index.html", context)


@router.get("/ui/screener/options", response_class=HTMLResponse)
def ui_screener_options(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    return templates.TemplateResponse(request, "index.html", _base_context(db, "screener-options"))


@router.get("/ui/screener/snipe/stocks", response_class=HTMLResponse)
def ui_screener_snipe_stocks(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    context = _base_context(db, "snipe-stocks")
    try:
        context["snipe_result"] = run_snipe_scan(db)
    except CostLimitExceededError as exc:
        context["snipe_error"] = exc.reason
    except Exception as exc:
        context["snipe_error"] = f"تعذّر تشغيل ماسح القنص: {exc}"
    return templates.TemplateResponse(request, "index.html", context)


@router.get("/ui/screener/snipe/options", response_class=HTMLResponse)
def ui_screener_snipe_options(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    context = _base_context(db, "snipe-options")
    try:
        context["snipe_options_result"] = run_snipe_options_scan(db)
    except CostLimitExceededError as exc:
        context["snipe_options_error"] = exc.reason
    except Exception as exc:
        context["snipe_options_error"] = f"تعذّر تشغيل قنص عقود الأوبشن: {exc}"
    return templates.TemplateResponse(request, "index.html", context)


@router.post("/ui/watchlist/add")
def ui_watchlist_add(body: WatchlistAddRequest, db: Session = Depends(get_db)) -> dict:
    """Adds one contract to the read-only "مراقب حالة قرائي" watchlist (owner request
    2026-07-19) — no trade execution here, just a DB row + an initial status reading."""
    try:
        item = add_watchlist_item(
            db, underlying_symbol=body.underlying_symbol, option_type=body.option_type,
            strike=body.strike, expiry=body.expiry, reference_price=body.reference_price,
            alert_threshold_pct=body.alert_threshold_pct, invalidation_price=body.invalidation_price,
        )
    except InvalidSymbolError as exc:
        return {"error": f"رمز غير صالح: {exc}"}
    except Exception as exc:
        return {"error": f"تعذّر إضافة العقد للمراقبة: {exc}"}
    return item.model_dump(mode="json")


@router.get("/ui/watchlist/list")
def ui_watchlist_list(db: Session = Depends(get_db)) -> dict:
    """Polled by the snipe-options tab's JS — refreshes every active watched contract's
    price and graduated status tier (each contract's own short cache TTL keeps repeated
    polls cheap; see `list_watchlist_with_status`)."""
    try:
        items = list_watchlist_with_status(db)
    except CostLimitExceededError as exc:
        return {"error": exc.reason, "items": []}
    except Exception as exc:
        return {"error": f"تعذّر تحديث قائمة المراقبة: {exc}", "items": []}
    return {"items": [i.model_dump(mode="json") for i in items]}


@router.post("/ui/watchlist/{item_id}/remove")
def ui_watchlist_remove(item_id: str, db: Session = Depends(get_db)) -> dict:
    import uuid as uuid_module

    try:
        parsed_id = uuid_module.UUID(item_id)
    except ValueError:
        return {"error": "invalid item_id"}
    try:
        remove_watchlist_item(db, parsed_id)
    except Exception as exc:
        return {"error": f"تعذّر إزالة العقد من المراقبة: {exc}"}
    return {"status": "removed"}


@router.get("/ui/watchlist/{item_id}/events")
def ui_watchlist_events(item_id: str, db: Session = Depends(get_db)) -> dict:
    import uuid as uuid_module

    try:
        parsed_id = uuid_module.UUID(item_id)
    except ValueError:
        return {"error": "invalid item_id", "events": []}
    events = get_watchlist_item_events(db, parsed_id)
    return {"events": [e.model_dump(mode="json") for e in events]}


@router.post("/ui/analyze-option", response_class=HTMLResponse)
def ui_analyze_option(
    request: Request, file: UploadFile = File(...), db: Session = Depends(get_db)
) -> HTMLResponse:
    option_result = None
    option_error = None
    try:
        if file.content_type not in ALLOWED_MEDIA_TYPES:
            raise ValueError(f"نوع صورة غير مدعوم: {file.content_type} (استخدم PNG أو JPEG أو WEBP)")
        image_bytes = file.file.read(MAX_IMAGE_BYTES + 1)
        if len(image_bytes) > MAX_IMAGE_BYTES:
            raise ValueError("حجم الصورة أكبر من الحد المسموح (8 ميجابايت)")
        option_result = run_option_image_analysis(db, image_bytes, file.content_type)
    except VisionExtractionError as exc:
        option_error = f"تعذّر قراءة بيانات العقد من الصورة بثقة كافية: {exc}"
    except DataFetchError as exc:
        option_error = f"تعذّر جلب بيانات السهم الأم للعقد: {exc}"
    except CostLimitExceededError as exc:
        option_error = exc.reason
    except ValueError as exc:
        option_error = str(exc)
    except Exception as exc:
        option_error = f"تعذّر إكمال تحليل العقد: {exc}"

    context = _base_context(db, "option")
    context.update(option_result=option_result, option_error=option_error)
    return templates.TemplateResponse(request, "index.html", context)
