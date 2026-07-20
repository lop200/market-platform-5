"""M2-B orchestration math (Annex C-2): ties vision extraction output + underlying data
into Greeks, Expected Move, translated stock levels, and a legal-safe Arabic report.

Pure computation only — no adapter or Cost Gate calls here (mirrors
`engines/deterministic/engine.py`). The caller (API route) is responsible for running the
vision call and the underlying-data fetch through the Cost Gate first, then passing their
results in here.
"""
from __future__ import annotations

from datetime import date

import pandas as pd

from app.engines.deterministic.indicators import compute_indicators
from app.engines.deterministic.levels import detect_levels
from app.engines.options.greeks import (
    compute_greeks,
    solve_implied_volatility,
    years_to_expiry,
)
from app.engines.options.iv_metrics import (
    compute_expected_move,
    daily_theta_decay_pct,
    translate_stock_level_to_contract_price,
)
from app.engines.options.schemas import (
    ExpectedMoveOutput,
    ExtractedContract,
    GreeksOutput,
    OptionContractAnalysis,
    TranslatedLevel,
)
from app.legal.disclaimers import BANNED_PHRASES_AR, DISCLAIMER_AR


def _build_report_ar(
    extracted: ExtractedContract,
    underlying_price: float,
    implied_vol: float,
    greeks: GreeksOutput,
    expected_move: ExpectedMoveOutput,
    translated_levels: list[TranslatedLevel],
    theta_pct: float | None,
    days_to_expiry: int,
) -> str:
    option_type_ar = "Call" if extracted.option_type == "call" else "Put"
    lines = [
        f"تحليل عقد {extracted.symbol} {option_type_ar} إضراب {extracted.strike:g} "
        f"(انتهاء {extracted.expiry.isoformat()}، {days_to_expiry} يوماً متبقياً).",
        f"سعر السهم الأم وقت التحليل: {underlying_price:.2f}. التقلب الضمني المستخرج من سعر "
        f"العقد الملاحَظ ({extracted.contract_price:.2f}): {implied_vol * 100:.1f}%.",
        f"الحركة المتوقعة (انحراف معياري واحد حتى الانتهاء): من {expected_move.lower_bound:.2f} "
        f"إلى {expected_move.upper_bound:.2f} ({expected_move.pct_of_price:.1f}% من سعر السهم).",
        f"حساسيات العقد الحالية — Delta: {greeks.delta:.3f}، Gamma: {greeks.gamma:.4f}، "
        f"Vega: {greeks.vega:.3f}.",
    ]
    if theta_pct is not None:
        lines.append(
            f"تآكل Theta اليومي التقديري: {greeks.theta:.3f}$ في اليوم (~{theta_pct:.2f}% من "
            "قيمة العقد الحالية) — هذا التآكل يستمر يومياً حتى الانتهاء بمعزل عن حركة السهم."
        )
    if translated_levels:
        lines.append("ترجمة أقرب المستويات الفنية للسهم الأم إلى سعر تقديري للعقد:")
        for level in translated_levels:
            lines.append(
                f"  - {level.label} عند {level.stock_price:.2f} للسهم "
                f"-> سعر عقد تقديري ~{level.estimated_contract_price:.2f}"
            )
    lines.append(
        "هذه أرقام مشتقة من نموذج Black-Scholes بافتراض ثبات التقلب الضمني والزمن المتبقي، "
        "وليست تنبؤاً بأداء العقد — القرار وتوقيته يعودان للمستخدم وحده."
    )
    if extracted.extraction_confidence == "low":
        lines.append(
            f"تنبيه: بعض بيانات العقد استُخرجت من الصورة بثقة منخفضة"
            + (f" ({extracted.raw_notes})" if extracted.raw_notes else "")
            + " — يُنصح بالتحقق منها يدوياً قبل الاعتماد عليها."
        )
    lines.append(DISCLAIMER_AR)
    return "\n".join(lines)


def _assert_no_banned_phrases(text: str) -> None:
    # Scan with the approved disclaimer text stripped out first — see the matching note
    # in report_engine.py's find_banned_phrases for why (avoids both substring collisions
    # like EN "recommendation"/"recommendations" and Arabic attached-clitic false negatives
    # that a naive word-boundary regex would introduce).
    scan_text = text.replace(DISCLAIMER_AR, "")
    for phrase in BANNED_PHRASES_AR:
        if phrase in scan_text:
            raise RuntimeError(f"banned phrase '{phrase}' found in a code-templated report — fix the template")


def analyze_option_contract(
    extracted: ExtractedContract,
    underlying_daily: pd.DataFrame,
    risk_free_rate: float,
    vision_cost_usd: float,
    as_of: date | None = None,
) -> OptionContractAnalysis:
    underlying_price = float(underlying_daily["close"].iloc[-1])

    indicators = compute_indicators(underlying_daily)
    levels = detect_levels(underlying_daily, atr_value=indicators.atr_14, current_price=underlying_price)

    t = years_to_expiry(extracted.expiry, as_of=as_of)
    days_to_expiry = round(t * 365)

    implied_vol = solve_implied_volatility(
        extracted.contract_price, underlying_price, extracted.strike, t, extracted.option_type, risk_free_rate
    )
    greeks_raw = compute_greeks(
        underlying_price, extracted.strike, t, implied_vol, extracted.option_type, risk_free_rate
    )
    greeks = GreeksOutput(delta=greeks_raw.delta, gamma=greeks_raw.gamma, theta=greeks_raw.theta, vega=greeks_raw.vega)

    expected_move_raw = compute_expected_move(underlying_price, implied_vol, t)
    expected_move = ExpectedMoveOutput(
        dollar_amount=expected_move_raw.dollar_amount,
        pct_of_price=expected_move_raw.pct_of_price,
        upper_bound=expected_move_raw.upper_bound,
        lower_bound=expected_move_raw.lower_bound,
    )

    translated_levels: list[TranslatedLevel] = []
    level_specs: list[tuple[str, float | None]] = []
    if levels.supports:
        level_specs.append(("دعم قوي", levels.supports[0].price))
    if levels.resistances:
        level_specs.append(("مقاومة قوية", levels.resistances[0].price))
    if levels.invalidation is not None:
        level_specs.append(("مستوى الإبطال الفني", levels.invalidation))

    for label, stock_price in level_specs:
        if stock_price is None:
            continue
        estimated_price = translate_stock_level_to_contract_price(
            stock_level_price=stock_price,
            current_underlying_price=underlying_price,
            current_contract_price=extracted.contract_price,
            delta=greeks.delta,
            gamma=greeks.gamma,
        )
        translated_levels.append(
            TranslatedLevel(label=label, stock_price=round(stock_price, 4), estimated_contract_price=estimated_price)
        )

    theta_pct = daily_theta_decay_pct(greeks.theta, extracted.contract_price)

    report_text_ar = _build_report_ar(
        extracted, underlying_price, implied_vol, greeks, expected_move, translated_levels, theta_pct, days_to_expiry
    )
    _assert_no_banned_phrases(report_text_ar)

    return OptionContractAnalysis(
        symbol=extracted.symbol,
        option_type=extracted.option_type,
        strike=extracted.strike,
        expiry=extracted.expiry,
        contract_price_at_analysis=extracted.contract_price,
        underlying_price=underlying_price,
        implied_volatility=round(implied_vol, 6),
        time_to_expiry_days=days_to_expiry,
        greeks=greeks,
        expected_move=expected_move,
        invalidation_level=levels.invalidation,
        translated_levels=translated_levels,
        daily_theta_decay_pct=theta_pct,
        extraction_confidence=extracted.extraction_confidence,
        report_text_ar=report_text_ar,
        disclaimer=DISCLAIMER_AR,
        cost_usd=round(vision_cost_usd, 5),
    )
