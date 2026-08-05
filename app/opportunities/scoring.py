from __future__ import annotations

from typing import Any


def _clamp(value: float) -> int:
    return max(0, min(100, round(value)))


def _reason(score: int, good: str, weak: str, threshold: int = 65) -> str:
    return good if score >= threshold else weak


def volume_score(relative_volume: float | int | None) -> int:
    """Score RVOL without allowing trend to hide missing participation."""
    rvol = max(0.0, float(relative_volume or 0))
    if rvol < 0.25:
        return 10
    if rvol < 0.5:
        return 25
    if rvol < 0.8:
        return 45
    if rvol < 1.0:
        return 60
    if rvol < 1.5:
        return 75
    if rvol < 2.0:
        return 88
    return 100


def build_stock_scorecard(
    *,
    indicators: dict[str, Any],
    strategy_match_pct: int,
    strategy_checks: list[dict[str, Any]] | None,
    spread_pct: float | int | None,
    dollar_volume: float | int | None,
    quote_age_seconds: float | int | None,
    data_valid: bool,
    news_risk: bool = False,
    timeframe_alignment: dict[str, str | None] | None = None,
) -> dict[str, Any]:
    """Return deterministic, auditable stock scores.

    The composite deliberately gives participation, breakout confirmation,
    spread and liquidity more weight than trend alone. It is a scorecard, not
    a calibrated probability of profit.
    """
    alignment = timeframe_alignment or {}
    aligned_values = [value for value in alignment.values() if value]
    if aligned_values:
        majority = max(aligned_values.count("صاعد"), aligned_values.count("هابط"))
        trend = _clamp(35 + 65 * majority / len(aligned_values))
    else:
        ema9, ema20, ema50 = (
            indicators.get("ema9"), indicators.get("ema20"), indicators.get("ema50")
        )
        ordered = (
            all(value is not None for value in (ema9, ema20, ema50))
            and (ema9 > ema20 > ema50 or ema9 < ema20 < ema50)
        )
        trend = 85 if ordered else 55 if ema9 is not None and ema20 is not None else 25

    rvol = max(0.0, float(indicators.get("relative_volume") or 0))
    volume = volume_score(rvol)
    checks = strategy_checks or []
    if checks:
        passed = sum(bool(item.get("passed")) for item in checks)
        breakout = _clamp(passed / len(checks) * 100)
    else:
        breakout = _clamp(strategy_match_pct * 0.75)

    spread = max(0.0, float(spread_pct if spread_pct is not None else 100))
    spread_quality = _clamp(100 - spread * 20)
    dollars = max(0.0, float(dollar_volume or 0))
    if dollars >= 100_000_000:
        liquidity = 100
    elif dollars >= 25_000_000:
        liquidity = 85
    elif dollars >= 5_000_000:
        liquidity = 70
    elif dollars >= 1_000_000:
        liquidity = 50
    elif dollars > 0:
        liquidity = 25
    else:
        liquidity = 0

    raw_entry = _clamp(
        trend * 0.20
        + volume * 0.25
        + breakout * 0.20
        + spread_quality * 0.15
        + liquidity * 0.20
    )
    quote_age = float(quote_age_seconds) if quote_age_seconds is not None else 10_000
    risk = _clamp(
        (100 - spread_quality) * 0.30
        + (100 - liquidity) * 0.25
        + (100 - volume) * 0.25
        + (20 if news_risk else 0)
        + (25 if not data_valid or quote_age > 30 else 0)
    )
    stock_confidence = _clamp(raw_entry * 0.85 + (100 - risk) * 0.15)
    caps: list[str] = []
    if rvol < 0.5:
        stock_confidence = min(stock_confidence, 49)
        caps.append("RVOL أقل من 0.5؛ خُفضت ثقة السهم تلقائيًا مهما كان توافق الاتجاه.")
    if not data_valid:
        stock_confidence = min(stock_confidence, 35)
        caps.append("البيانات غير صالحة لخطة حية؛ الدرجة للمراقبة فقط.")

    reasons = {
        "trend": _reason(trend, "اتجاه الأطر متماسك.", "اتجاه الأطر متضارب أو غير مكتمل."),
        "volume": f"RVOL الحالي {rvol:.2f}; " + (
            "المشاركة تؤكد الحركة." if rvol >= 1 else "المشاركة دون المتوسط وتضعف الثقة."
        ),
        "breakout": _reason(breakout, "شروط الاختراق متحققة بدرجة جيدة.", "تأكيد الاختراق غير مكتمل."),
        "spread": f"السبريد {spread:.2f}%؛ " + (
            "تكلفة التنفيذ منضبطة." if spread_quality >= 65 else "تكلفة التنفيذ مرتفعة نسبيًا."
        ),
        "liquidity": f"Dollar Volume نحو ${dollars:,.0f}؛ " + (
            "السيولة داعمة." if liquidity >= 65 else "السيولة لا تدعم ثقة مرتفعة."
        ),
        "risk": "درجة مخاطرة أعلى تعني قيودًا أكثر على التنفيذ.",
        "entry_conditions": "مقياس اكتمال شروط الدخول الحالية، وليس احتمال ربح.",
        "trade_success": "غير محسوب: لا يوجد نموذج تاريخي معاير لاحتمال نجاح الصفقة.",
        "final_confidence": "ناقص حتى تتوفر بيانات عقد أوبشن صالح وحديث.",
    }
    return {
        "trend_score": trend,
        "volume_score": volume,
        "breakout_quality_score": breakout,
        "spread_score": spread_quality,
        "liquidity_score": liquidity,
        "risk_score": risk,
        "entry_conditions_score": raw_entry,
        "stock_confidence_score": stock_confidence,
        "trade_success_probability_pct": None,
        "trade_success_status": "unscored",
        "options_quality_score": None,
        "final_confidence_score": None,
        "final_confidence_status": "incomplete_options_data",
        "evaluation_complete": False,
        "reasons_ar": reasons,
        "caps_applied_ar": caps,
        "disclaimer_ar": "كل القيم درجات مقارنة من 100 وليست احتمال ربح أو ضمان نتيجة.",
    }


def finalize_scorecard_with_options(
    scorecard: dict[str, Any], options: dict[str, Any] | None
) -> dict[str, Any]:
    """Add a fresh ranked contract to the stock score without inventing data."""
    result = dict(scorecard)
    result["reasons_ar"] = dict(scorecard.get("reasons_ar") or {})
    ranked = list((options or {}).get("ranked_contracts") or [])
    fresh = [
        item for item in ranked
        if str(item.get("feed") or "").lower() == "opra"
        and item.get("quote_age_seconds") is not None
        and float(item["quote_age_seconds"]) <= 30
        and all(item.get(key) is not None for key in ("iv", "delta", "theta", "gamma", "dte"))
    ]
    if not fresh:
        result["options_quality_score"] = None
        result["final_confidence_score"] = None
        result["final_confidence_status"] = "incomplete_options_data"
        result["evaluation_complete"] = False
        result["reasons_ar"]["options"] = "بيانات الأوبشن غير متوفرة أو غير حديثة؛ التقييم النهائي ناقص."
        result["reasons_ar"]["final_confidence"] = "لا توجد درجة نهائية قوية دون عقد OPRA حديث ومكتمل Greeks."
        return result

    best = fresh[0]
    components = best.get("ranking_components") or {}
    option_quality = int(
        components.get("options_quality")
        or best.get("options_quality_score")
        or best.get("suitability_score")
        or 0
    )
    result["options_quality_score"] = _clamp(option_quality)
    result["final_confidence_score"] = _clamp(
        float(result.get("stock_confidence_score") or 0) * 0.65
        + option_quality * 0.25
        + (100 - float(result.get("risk_score") or 100)) * 0.10
    )
    if result.get("caps_applied_ar"):
        result["final_confidence_score"] = min(result["final_confidence_score"], 49)
    result["final_confidence_status"] = "complete"
    result["evaluation_complete"] = True
    result["reasons_ar"]["options"] = (
        "جودة أفضل عقد تحسب IV وDelta وTheta وGamma وDTE والسيولة والسبريد."
    )
    result["reasons_ar"]["final_confidence"] = (
        "مزيج من ثقة السهم 65% وجودة العقد 25% وضبط المخاطر 10%."
    )
    return result
