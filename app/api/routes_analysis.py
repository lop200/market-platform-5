"""POST /analyze, GET /report/{id}, GET /history (SRS section 8)."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.deps import require_api_key
from app.api.schemas import AnalyzeRequest, AnalyzeResponse, HistoryItem, NotableLevels, ScoresOut
from app.core.orchestrator import CostLimitExceededError, DataFetchError, InvalidSymbolError, run_analysis
from app.db import repository
from app.db.session import get_db
from app.engines.deterministic.plain_summary import build_plain_summary
from app.engines.deterministic.scenario_probabilities import compute_scenario_probabilities
from app.engines.deterministic.schemas import DeterministicAnalysis, Indicators
from app.engines.llm.report_engine import split_report_sections
from app.legal.disclaimers import DISCLAIMER_AR, DISCLAIMER_EN

router = APIRouter(prefix="/api/v1", tags=["analysis"], dependencies=[Depends(require_api_key)])


@router.post("/analyze")
def analyze(body: AnalyzeRequest, db: Session = Depends(get_db)) -> dict:
    try:
        response = run_analysis(db, body.symbol, lang=body.lang)
    except InvalidSymbolError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except DataFetchError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    except CostLimitExceededError as exc:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, exc.reason) from exc
    return response.model_dump(mode="json")


def _record_to_response(record) -> AnalyzeResponse:
    deterministic = record.deterministic_json
    levels = deterministic.get("levels", {})
    lang = "ar" if record.report_text_ar else "en"
    stored_text = record.report_text_ar or record.report_text_en or ""
    sections = split_report_sections(stored_text)
    is_ar = lang == "ar"
    # Historical view, no live re-fetch (see chart_bars note below) -> open price isn't
    # known here, so the plain-language summary falls back to open == last_close (0%
    # change) rather than fabricating an intraday number.
    analysis = DeterministicAnalysis.model_validate(deterministic)
    plain_summary = build_plain_summary(
        analysis, open_price=deterministic.get("last_close", 0.0)
    )
    scenario_probabilities = (
        analysis.scenario_probabilities or compute_scenario_probabilities(analysis)
    )

    return AnalyzeResponse(
        analysis_id=str(record.id),
        symbol=record.symbol,
        from_cache=False,
        regime=record.regime,
        last_close=deterministic.get("last_close", 0.0),
        scores=ScoresOut(
            technical=record.scores["technical"],
            volatility=record.scores["volatility"],
            liquidity=record.scores["liquidity"],
            risk=record.scores["risk"],
            overall_confidence=record.scores["overall_confidence"],
        ),
        notable_levels=NotableLevels(
            supports=[lv["price"] for lv in levels.get("supports", [])],
            resistances=[lv["price"] for lv in levels.get("resistances", [])],
            invalidation=levels.get("invalidation"),
        ),
        indicators=Indicators.model_validate(deterministic["indicators"]),
        scenario_probabilities=scenario_probabilities,
        plain_summary=plain_summary,
        chart_bars=[],  # historical view — no live OHLCV re-fetch to keep this endpoint free
        tldr_ar=sections["tldr"] if is_ar else None,
        tldr_en=sections["tldr"] if not is_ar else None,
        scenario_bullish_ar=sections["scenario_bullish"] if is_ar else None,
        scenario_bullish_en=sections["scenario_bullish"] if not is_ar else None,
        scenario_bearish_ar=sections["scenario_bearish"] if is_ar else None,
        scenario_bearish_en=sections["scenario_bearish"] if not is_ar else None,
        scenario_neutral_ar=sections["scenario_neutral"] if is_ar else None,
        scenario_neutral_en=sections["scenario_neutral"] if not is_ar else None,
        report_ar=sections["full_report"] if is_ar else None,
        report_en=sections["full_report"] if not is_ar else None,
        devils_advocate_ar=sections["devils_advocate"] if is_ar else None,
        devils_advocate_en=sections["devils_advocate"] if not is_ar else None,
        disclaimer=DISCLAIMER_AR if lang == "ar" else DISCLAIMER_EN,
        cost_usd=float(record.total_cost_usd),
        market_open=record.market_open,
        data_quality=deterministic.get("data_quality", "daily_only"),
        data_provider=record.data_provider,
        data_as_of=deterministic.get("data_as_of", record.requested_at.date().isoformat()),
    )


@router.get("/report/{analysis_id}")
def get_report(analysis_id: str, db: Session = Depends(get_db)) -> dict:
    try:
        parsed_id = uuid.UUID(analysis_id)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid analysis_id") from exc

    record = repository.get_analysis(db, parsed_id)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "analysis not found")

    return _record_to_response(record).model_dump(mode="json")


@router.get("/history")
def get_history(
    symbol: str = Query(...), limit: int = Query(default=20, le=100), db: Session = Depends(get_db)
) -> list[dict]:
    records = repository.get_analysis_history(db, symbol.strip().upper(), limit=limit)
    return [
        HistoryItem(
            analysis_id=str(r.id),
            symbol=r.symbol,
            requested_at=r.requested_at.isoformat(),
            regime=r.regime,
            overall_confidence=r.scores.get("overall_confidence", 0.0),
            from_cache=r.from_cache,
            status=r.status,
        ).model_dump(mode="json")
        for r in records
    ]
