"""Market regime classification — declared numeric rules (SRS 10.5):

    trending_up:   Close > EMA50 > EMA200, EMA50 slope positive, ADX(14) > 20
    trending_down: the mirror image of the above
    high_vol:      ATR% current > 1.5 x ATR% 90-day average  <- takes priority over the rest
    ranging:       none of the above

`reasons` always lists which explicit criteria fired, so the UI's "لماذا هذه القراءة؟"
button has real numbers to show (SRS P3 explainability).
"""
from __future__ import annotations

import pandas as pd

from app.engines.deterministic.indicators import ema
from app.engines.deterministic.schemas import Indicators, Regime, Volatility

ADX_TRENDING_THRESHOLD = 20
HIGH_VOL_MULTIPLE = 1.5
EMA50_SLOPE_LOOKBACK_BARS = 5


def classify_regime(
    daily: pd.DataFrame,
    indicators: Indicators,
    volatility: Volatility,
    slope_lookback: int = EMA50_SLOPE_LOOKBACK_BARS,
) -> Regime:
    last_close = float(daily["close"].iloc[-1])
    ema_50 = indicators.moving_averages.ema_50
    ema_200 = indicators.moving_averages.ema_200
    adx_14 = indicators.adx_14

    if (
        volatility.atr_pct_avg_90d
        and volatility.atr_pct_current > HIGH_VOL_MULTIPLE * volatility.atr_pct_avg_90d
    ):
        return Regime(
            label="high_vol",
            reasons=[
                f"ATR% الحالي ({volatility.atr_pct_current:.2f}) > "
                f"{HIGH_VOL_MULTIPLE}×متوسط 90 يوم ({volatility.atr_pct_avg_90d:.2f})"
            ],
        )

    if ema_50 is None or ema_200 is None:
        return Regime(label="ranging", reasons=["بيانات غير كافية لحساب EMA50/EMA200"])

    ema_50_series = ema(daily["close"], 50)
    if len(ema_50_series) > slope_lookback:
        ema_50_slope = float(ema_50_series.iloc[-1] - ema_50_series.iloc[-1 - slope_lookback])
    else:
        ema_50_slope = 0.0

    if last_close > ema_50 > ema_200 and ema_50_slope > 0 and adx_14 > ADX_TRENDING_THRESHOLD:
        return Regime(
            label="trending_up",
            reasons=[
                f"Close({last_close:.2f}) > EMA50({ema_50:.2f}) > EMA200({ema_200:.2f})",
                f"ميل EMA50 موجب ({ema_50_slope:.4f})",
                f"ADX(14)={adx_14:.2f} > {ADX_TRENDING_THRESHOLD}",
            ],
        )

    if last_close < ema_50 < ema_200 and ema_50_slope < 0 and adx_14 > ADX_TRENDING_THRESHOLD:
        return Regime(
            label="trending_down",
            reasons=[
                f"Close({last_close:.2f}) < EMA50({ema_50:.2f}) < EMA200({ema_200:.2f})",
                f"ميل EMA50 سالب ({ema_50_slope:.4f})",
                f"ADX(14)={adx_14:.2f} > {ADX_TRENDING_THRESHOLD}",
            ],
        )

    return Regime(label="ranging", reasons=["لم تتحقق شروط الاتجاه الصاعد أو الهابط أو التقلب المرتفع"])
