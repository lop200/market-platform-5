"""Weekly Structure Score (WSS) — new screener formula (not in SRS section 12, declared
here per CLAUDE.md rule 2). Weekly-timeframe structural strength, 0-100:

    WSS = 35 x above_ma_component + 35 x higher_highs_component + 30 x volume_trend_component

- above_ma_component: 1.0 if the last weekly close is above BOTH the 20-week and 50-week
  EMA, 0.5 above only one, 0.0 above neither ("فوق متوسطاتها").
- higher_highs_component: 1.0 if the max weekly high of the last 4 weeks exceeds the max
  weekly high of the preceding 4 weeks, else 0.0 ("Higher Highs").
- volume_trend_component: the % change of the last-4-week average volume vs the preceding
  4 weeks, scaled to 1.0 at +50% or more ("حجم متصاعد").

Computed directly from daily OHLCV resampled to weekly bars (Friday close) — no LLM
(CLAUDE.md rule 1), independent of the daily-timeframe deterministic engine.
"""
from __future__ import annotations

import pandas as pd

ABOVE_MA_WEIGHT = 35
HIGHER_HIGHS_WEIGHT = 35
VOLUME_TREND_WEIGHT = 30

VOLUME_TREND_SATURATION = 0.5  # +50% week-over-4-week volume growth maxes the component
MIN_WEEKLY_BARS = 10


def resample_weekly(daily: pd.DataFrame) -> pd.DataFrame:
    weekly = daily.resample("W-FRI").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    )
    return weekly.dropna()


def compute_weekly_structure(daily: pd.DataFrame) -> tuple[float, list[str]]:
    weekly = resample_weekly(daily)
    if len(weekly) < MIN_WEEKLY_BARS:
        return 0.0, ["بيانات أسبوعية غير كافية لتقييم البنية"]

    close = weekly["close"]
    last_close = float(close.iloc[-1])
    ema20 = close.ewm(span=20, adjust=False).mean()
    above20 = last_close > float(ema20.iloc[-1])

    if len(weekly) >= 50:
        ema50 = close.ewm(span=50, adjust=False).mean()
        above50 = last_close > float(ema50.iloc[-1])
        above_ma_component = 1.0 if (above20 and above50) else (0.5 if (above20 or above50) else 0.0)
    else:
        above50 = None
        above_ma_component = 1.0 if above20 else 0.0

    recent_high = float(weekly["high"].iloc[-4:].max())
    prior_high = float(weekly["high"].iloc[-8:-4].max())
    higher_highs = recent_high > prior_high
    higher_highs_component = 1.0 if higher_highs else 0.0

    recent_vol = float(weekly["volume"].iloc[-4:].mean())
    prior_vol = float(weekly["volume"].iloc[-8:-4].mean())
    vol_change = (recent_vol / prior_vol - 1) if prior_vol > 0 else 0.0
    volume_trend_component = min(max(vol_change / VOLUME_TREND_SATURATION, 0.0), 1.0)

    score = (
        ABOVE_MA_WEIGHT * above_ma_component
        + HIGHER_HIGHS_WEIGHT * higher_highs_component
        + VOLUME_TREND_WEIGHT * volume_trend_component
    )

    reasons: list[str] = []
    if above_ma_component == 1.0:
        reasons.append("فوق المتوسطين الأسبوعيين 20 و50" if above50 is not None else "فوق المتوسط الأسبوعي 20")
    elif above_ma_component == 0.5:
        reasons.append("فوق أحد المتوسطين الأسبوعيين فقط")
    if higher_highs:
        reasons.append("قمم أسبوعية أعلى من سابقتها (Higher Highs)")
    if volume_trend_component > 0.5:
        reasons.append("حجم تداول أسبوعي متصاعد")
    if not reasons:
        reasons.append("لا توجد إشارات بنية أسبوعية قوية بارزة حالياً")

    return round(score, 1), reasons
