"""Daily Technical Readiness Score for the Snipe scanner ("درجة الجاهزية الفنية اليومية") —
new formula, not in SRS section 12 (declared and documented here per CLAUDE.md rule 2).
Momentum + unusual volume + proximity to a notable level + timeframe confluence, 0-100:

    SnipeScore = 25 x momentum_component + 20 x rvol_component
               + 20 x proximity_component + 20 x confluence_component
               + 15 x smc_confirmation_component

The SMC component averages directional agreement from RSI divergence,
accumulation/distribution, and the latest order block. Missing or neutral evidence
contributes 0.5 instead of being treated as confirmation.

- momentum_component / rvol_component / proximity_component: identical logic to the
  existing Daily Readiness Score (`screener/daily_readiness.py`) — 60% RSI distance from
  the neutral 50 line (capped at 30 points away) + 40% MACD histogram rising; RVOL scaled
  to 1.0 at 3x the 20-day average; distance (in ATR multiples) to the nearer of the
  nearest support/resistance, 0 ATR away -> 1.0, 3+ ATR away -> 0.0.
- confluence_component (new — "توافق فريمات"): compares the daily regime direction
  (`Regime.label`) against the weekly-timeframe direction derived from
  `screener/weekly_structure.resample_weekly` (last weekly close vs its 20-week EMA).
  1.0 if both timeframes agree on direction (up/up or down/down), 0.5 if the weekly
  read is flat/ranging or there isn't enough weekly history yet, 0.0 on outright conflict.

No LLM involved (CLAUDE.md rule 1) — pure arithmetic on the already-computed deterministic
analysis plus the same daily OHLCV frame, same objects the screener already works with.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import pandas as pd

from app.engines.deterministic.schemas import DeterministicAnalysis
from app.engines.screener.weekly_structure import MIN_WEEKLY_BARS, resample_weekly

MOMENTUM_WEIGHT = 25
RVOL_WEIGHT = 20
PROXIMITY_WEIGHT = 20
CONFLUENCE_WEIGHT = 20
SMC_CONFIRMATION_WEIGHT = 15

RVOL_SATURATION = 3.0
RSI_DISTANCE_SATURATION = 30.0
PROXIMITY_SATURATION_ATR = 3.0

WeeklyDirection = Literal["up", "down", "flat"]
SnipeDirection = Literal["bullish", "bearish", "neutral"]


@dataclass(frozen=True)
class SnipeStrategy:
    name_ar: str
    description_ar: str


def _weekly_direction(daily: pd.DataFrame) -> WeeklyDirection | None:
    """None means "not enough weekly history to judge" (distinct from a genuine "flat")."""
    weekly = resample_weekly(daily)
    if len(weekly) < MIN_WEEKLY_BARS:
        return None
    close = weekly["close"]
    last_close = float(close.iloc[-1])
    ema20 = close.ewm(span=20, adjust=False).mean()
    last_ema20 = float(ema20.iloc[-1])
    if last_close > last_ema20:
        return "up"
    if last_close < last_ema20:
        return "down"
    return "flat"


def _confluence_component(regime_label: str, weekly_direction: WeeklyDirection | None) -> float:
    if weekly_direction is None or weekly_direction == "flat":
        return 0.5
    daily_direction = "up" if regime_label == "trending_up" else ("down" if regime_label == "trending_down" else None)
    if daily_direction is None:
        return 0.5
    return 1.0 if daily_direction == weekly_direction else 0.0


def infer_snipe_direction(
    analysis: DeterministicAnalysis, daily: pd.DataFrame
) -> SnipeDirection:
    """Infer direction before any option type is considered.

    Daily/weekly conflict forces abstention. Otherwise a direction needs at least three
    weighted votes and a two-vote lead, preventing a single indicator from choosing a
    Call or Put.
    """
    weekly = _weekly_direction(daily)
    daily_direction = (
        "up"
        if analysis.regime.label == "trending_up"
        else "down"
        if analysis.regime.label == "trending_down"
        else None
    )
    if daily_direction and weekly in {"up", "down"} and daily_direction != weekly:
        return "neutral"

    bullish = bearish = 0
    if daily_direction == "up":
        bullish += 2
    elif daily_direction == "down":
        bearish += 2
    if weekly == "up":
        bullish += 2
    elif weekly == "down":
        bearish += 2

    histogram = analysis.indicators.macd.histogram
    if histogram > 0:
        bullish += 1
    elif histogram < 0:
        bearish += 1
    if analysis.indicators.rsi_14 >= 55:
        bullish += 1
    elif analysis.indicators.rsi_14 <= 45:
        bearish += 1

    ema20 = analysis.indicators.moving_averages.ema_20
    if ema20 is not None:
        if analysis.last_close > ema20:
            bullish += 1
        elif analysis.last_close < ema20:
            bearish += 1

    divergence = analysis.smc.rsi_divergence
    if divergence.detected and divergence.type == "bullish":
        bullish += 2
    elif divergence.detected and divergence.type == "bearish":
        bearish += 2

    ad_read = analysis.smc.accumulation_distribution.interpretation
    if ad_read == "accumulation":
        bullish += 1
    elif ad_read == "distribution":
        bearish += 1

    if analysis.smc.order_blocks:
        latest_block = max(analysis.smc.order_blocks, key=lambda block: block.bar_index)
        if latest_block.direction == "bullish":
            bullish += 1
        elif latest_block.direction == "bearish":
            bearish += 1

    if bullish >= 3 and bullish - bearish >= 2:
        return "bullish"
    if bearish >= 3 and bearish - bullish >= 2:
        return "bearish"
    return "neutral"


def _smc_confirmation_component(
    analysis: DeterministicAnalysis, direction: SnipeDirection
) -> float:
    if direction == "neutral":
        return 0.5
    expected = "bullish" if direction == "bullish" else "bearish"
    readings: list[float] = []

    divergence = analysis.smc.rsi_divergence
    if divergence.detected and divergence.type:
        readings.append(1.0 if divergence.type == expected else 0.0)

    ad_read = analysis.smc.accumulation_distribution.interpretation
    if ad_read == "neutral":
        readings.append(0.5)
    else:
        aligned_ad = (
            ad_read == "accumulation" if direction == "bullish" else ad_read == "distribution"
        )
        readings.append(1.0 if aligned_ad else 0.0)

    if analysis.smc.order_blocks:
        latest_block = max(analysis.smc.order_blocks, key=lambda block: block.bar_index)
        readings.append(1.0 if latest_block.direction == expected else 0.0)

    return sum(readings) / len(readings) if readings else 0.5


def select_snipe_strategy(
    analysis: DeterministicAnalysis,
    daily: pd.DataFrame,
    direction: SnipeDirection | None = None,
) -> SnipeStrategy:
    direction = direction or infer_snipe_direction(analysis, daily)
    weekly = _weekly_direction(daily)
    confluence = _confluence_component(analysis.regime.label, weekly)
    histogram = analysis.indicators.macd.histogram
    strengthening = (
        histogram > 0 and analysis.indicators.macd.histogram_rising
        if direction == "bullish"
        else histogram < 0 and not analysis.indicators.macd.histogram_rising
    )

    if analysis.regime.label in {"trending_up", "trending_down"} and confluence == 1.0:
        return SnipeStrategy(
            name_ar="استراتيجية استمرار الاتجاه متعدد الفريمات",
            description_ar=(
                "تتطلب توافق الاتجاه اليومي والأسبوعي، ثم تؤكد الزخم والحجم وإشارات SMC "
                "قبل ترتيب المستوى والعقد."
            ),
        )
    if analysis.liquidity.rvol >= 1.5 and strengthening:
        return SnipeStrategy(
            name_ar="استراتيجية توسع الزخم والحجم",
            description_ar=(
                "تعتمد على تسارع MACD مع حجم نسبي مرتفع وقرب مستوى فني، مع استبعاد "
                "الاتجاه المتعارض بين الفريمات."
            ),
        )
    return SnipeStrategy(
        name_ar="استراتيجية تفاعل المستوى بتأكيد SMC",
        description_ar=(
            "تعتمد على قرب السعر من مستوى فني، واتجاه RSI وMACD، والتجميع أو التصريف "
            "والدايفرجنس والـOrder Block عند توفرها."
        ),
    )


def compute_snipe_score(analysis: DeterministicAnalysis, daily: pd.DataFrame) -> tuple[float, list[str]]:
    direction = infer_snipe_direction(analysis, daily)
    rvol = analysis.liquidity.rvol
    rvol_component = min(rvol / RVOL_SATURATION, 1.0)

    rsi = analysis.indicators.rsi_14
    histogram = analysis.indicators.macd.histogram
    if direction == "bullish":
        rsi_strength = min(max(rsi - 50, 0) / RSI_DISTANCE_SATURATION, 1.0)
        macd_strength = 1.0 if histogram > 0 and analysis.indicators.macd.histogram_rising else (0.4 if histogram > 0 else 0.0)
    elif direction == "bearish":
        rsi_strength = min(max(50 - rsi, 0) / RSI_DISTANCE_SATURATION, 1.0)
        macd_strength = 1.0 if histogram < 0 and not analysis.indicators.macd.histogram_rising else (0.4 if histogram < 0 else 0.0)
    else:
        rsi_strength = 0.0
        macd_strength = 0.0
    momentum_component = 0.6 * rsi_strength + 0.4 * macd_strength

    atr = analysis.indicators.atr_14 or 1e-9
    distances: list[float] = []
    if analysis.levels.supports:
        nearest_support = max(lv.price for lv in analysis.levels.supports)
        distances.append(abs(analysis.last_close - nearest_support) / atr)
    if analysis.levels.resistances:
        nearest_resistance = min(lv.price for lv in analysis.levels.resistances)
        distances.append(abs(nearest_resistance - analysis.last_close) / atr)
    nearest_distance_atr = min(distances) if distances else PROXIMITY_SATURATION_ATR
    proximity_component = max(0.0, 1 - nearest_distance_atr / PROXIMITY_SATURATION_ATR)

    weekly_direction = _weekly_direction(daily)
    confluence_component = _confluence_component(analysis.regime.label, weekly_direction)
    smc_component = _smc_confirmation_component(analysis, direction)

    score = (
        MOMENTUM_WEIGHT * momentum_component
        + RVOL_WEIGHT * rvol_component
        + PROXIMITY_WEIGHT * proximity_component
        + CONFLUENCE_WEIGHT * confluence_component
        + SMC_CONFIRMATION_WEIGHT * smc_component
    )

    reasons: list[str] = []
    strategy = select_snipe_strategy(analysis, daily, direction)
    reasons.append(f"الاستراتيجية: {strategy.name_ar}")
    if direction == "bullish":
        reasons.append("اتجاه صاعد مكتمل قبل فحص عقود Call")
    elif direction == "bearish":
        reasons.append("اتجاه هابط مكتمل قبل فحص عقود Put")
    else:
        reasons.append("اتجاه غير مكتمل — لا يُختار له عقد أوبشن")
    if rvol > 2.5:
        reasons.append(f"حجم غير طبيعي: {rvol:.1f}× المعتاد")
    elif rvol > 1.5:
        reasons.append(f"حجم أعلى من المعتاد: {rvol:.1f}×")
    if direction == "bullish" and histogram > 0 and analysis.indicators.macd.histogram_rising:
        reasons.append("زخم MACD الإيجابي يتقوى")
    elif direction == "bearish" and histogram < 0 and not analysis.indicators.macd.histogram_rising:
        reasons.append("زخم MACD السلبي يتقوى")
    if nearest_distance_atr < 1.0:
        reasons.append("قريب جداً من مستوى فني مفصلي")
    if confluence_component == 1.0:
        reasons.append("توافق بين الاتجاه اليومي والأسبوعي")
    elif confluence_component == 0.0:
        reasons.append("تعارض بين الاتجاه اليومي والأسبوعي")
    if not reasons:
        reasons.append("لا توجد إشارات جاهزية بارزة اليوم")

    return round(score, 1), reasons
