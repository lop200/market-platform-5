"""Deterministic warning-list extraction feeding the mandatory critical-review paragraph
(SRS 14). The LLM only phrases these numeric alerts critically — it never invents them
(SRS 14.2). If the list comes back empty, SRS 14.3 requires saying so explicitly rather
than manufacturing fake criticism; `format_alerts_for_prompt` handles that fallback line.
"""
from __future__ import annotations

import pandas as pd

from app.engines.deterministic.schemas import DeterministicAnalysis

# Declared, calibratable thresholds (SRS 14.2 names the checks, not exact constants).
RSI_EXTREME_LOW = 30
RSI_EXTREME_HIGH = 75
EMA20_EXTENSION_ATR_MULTIPLE = 2.0
RESISTANCE_PROXIMITY_ATR_MULTIPLE = 1.0
HIGH_RISK_SCORE_THRESHOLD = 65
WEAK_LIQUIDITY_SCORE_THRESHOLD = 40
COMPRESSED_BANDWIDTH_PERCENTILE = 10
VOLUME_DIVERGENCE_LOOKBACK_DAYS = 5
VOLUME_DIVERGENCE_DROP_PCT = 10  # recent avg volume must be at least this much lower

NO_ALERTS_FALLBACK_AR = (
    "لم يرصد المحرك إنذارات بنيوية بارزة — وهذا بحد ذاته يستدعي الحذر من الرضا الزائد."
)


def _check_resistance_proximity(analysis: DeterministicAnalysis) -> str | None:
    if not analysis.levels.resistances:
        return None
    nearest = analysis.levels.resistances[0]
    distance = nearest.price - analysis.last_close
    if 0 <= distance <= RESISTANCE_PROXIMITY_ATR_MULTIPLE * analysis.indicators.atr_14:
        return (
            f"مقاومة قوية قريبة جداً عند {nearest.price:.2f} "
            f"(أقل من {RESISTANCE_PROXIMITY_ATR_MULTIPLE:g}×ATR من السعر الحالي)"
        )
    return None


def _check_rsi_extreme(analysis: DeterministicAnalysis) -> str | None:
    rsi = analysis.indicators.rsi_14
    if rsi < RSI_EXTREME_LOW:
        return f"RSI في منطقة تشبع بيعي متطرفة ({rsi:.1f})"
    if rsi > RSI_EXTREME_HIGH:
        return f"RSI في منطقة تشبع شرائي متطرفة ({rsi:.1f})"
    return None


def _check_volume_divergence(daily: pd.DataFrame) -> str | None:
    n = VOLUME_DIVERGENCE_LOOKBACK_DAYS
    if len(daily) < 2 * n:
        return None
    recent_close, prior_close = daily["close"].iloc[-1], daily["close"].iloc[-1 - n]
    recent_vol = daily["volume"].iloc[-n:].mean()
    prior_vol = daily["volume"].iloc[-2 * n : -n].mean()
    price_rising = recent_close > prior_close
    volume_dropping = prior_vol > 0 and recent_vol <= prior_vol * (1 - VOLUME_DIVERGENCE_DROP_PCT / 100)
    if price_rising and volume_dropping:
        return "تباعد حجمي: السعر يصعد بينما الحجم النسبي يتراجع — دعم ضعيف للاندفاع الحالي"
    return None


def _check_ema20_extension(analysis: DeterministicAnalysis) -> str | None:
    ema_20 = analysis.indicators.moving_averages.ema_20
    if ema_20 is None:
        return None
    distance = abs(analysis.last_close - ema_20)
    if distance > EMA20_EXTENSION_ATR_MULTIPLE * analysis.indicators.atr_14:
        return f"السعر ممدود عن EMA20 بأكثر من {EMA20_EXTENSION_ATR_MULTIPLE:g}×ATR — عرضة لارتداد تصحيحي"
    return None


def _check_high_risk_score(analysis: DeterministicAnalysis) -> str | None:
    if analysis.scores.risk > HIGH_RISK_SCORE_THRESHOLD:
        return f"درجة المخاطرة مرتفعة ({analysis.scores.risk:.0f}/100)"
    return None


def _check_weak_liquidity(analysis: DeterministicAnalysis) -> str | None:
    if analysis.scores.liquidity < WEAK_LIQUIDITY_SCORE_THRESHOLD:
        return f"سيولة ضعيفة نسبياً (درجة السيولة {analysis.scores.liquidity:.0f}/100)"
    return None


def _check_compressed_volatility(analysis: DeterministicAnalysis) -> str | None:
    percentile = analysis.indicators.bollinger.bandwidth_percentile_90d
    if percentile is not None and percentile < COMPRESSED_BANDWIDTH_PERCENTILE:
        return "تقلب منضغط (Bollinger Bandwidth في أدنى 10% تاريخياً) — قابل للانفجار في أي اتجاه"
    return None


def generate_devils_advocate_alerts(daily: pd.DataFrame, analysis: DeterministicAnalysis) -> list[str]:
    checks = (
        _check_resistance_proximity(analysis),
        _check_rsi_extreme(analysis),
        _check_volume_divergence(daily),
        _check_ema20_extension(analysis),
        _check_high_risk_score(analysis),
        _check_weak_liquidity(analysis),
        _check_compressed_volatility(analysis),
    )
    return [alert for alert in checks if alert is not None]


def format_alerts_for_prompt(alerts: list[str]) -> str:
    if not alerts:
        return NO_ALERTS_FALLBACK_AR
    return "\n".join(f"- {alert}" for alert in alerts)
