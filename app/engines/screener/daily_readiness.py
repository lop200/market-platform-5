"""Daily Technical Readiness Score (DRS) — new screener formula (not in SRS section 12,
declared and documented here per CLAUDE.md rule 2: every score is an explicit, inspectable
formula). Momentum + unusual volume + proximity to a notable technical level, 0-100.

    DRS = 40 x volume_component + 35 x momentum_component + 25 x proximity_component

- volume_component: RVOL scaled to 1.0 at 3x the 20-day average volume (already flagged
  "unusual" by the deterministic engine above 2.5x — SRS Annex C-5).
- momentum_component: 60% RSI distance from the neutral 50 line (capped at 30 points away),
  40% whether the MACD histogram is rising (momentum accelerating in its current direction).
- proximity_component: how close price is (in ATR multiples) to the nearer of the nearest
  support/resistance — a setup right at a notable level is more "ready" to react than one
  drifting mid-range. 0 ATR away -> 1.0, 3+ ATR away -> 0.0.

No LLM involved (CLAUDE.md rule 1) — pure arithmetic on the already-computed deterministic
analysis, same object `run_analysis` produces for a single-symbol report.
"""
from __future__ import annotations

from app.engines.deterministic.schemas import DeterministicAnalysis

VOLUME_WEIGHT = 40
MOMENTUM_WEIGHT = 35
PROXIMITY_WEIGHT = 25

RVOL_SATURATION = 3.0
RSI_DISTANCE_SATURATION = 30.0
PROXIMITY_SATURATION_ATR = 3.0


def compute_daily_readiness(analysis: DeterministicAnalysis) -> tuple[float, list[str]]:
    rvol = analysis.liquidity.rvol
    volume_component = min(rvol / RVOL_SATURATION, 1.0)

    rsi = analysis.indicators.rsi_14
    rsi_strength = min(abs(rsi - 50) / RSI_DISTANCE_SATURATION, 1.0)
    macd_strength = 1.0 if analysis.indicators.macd.histogram_rising else 0.4
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

    score = (
        VOLUME_WEIGHT * volume_component
        + MOMENTUM_WEIGHT * momentum_component
        + PROXIMITY_WEIGHT * proximity_component
    )

    reasons: list[str] = []
    if rvol > 2.5:
        reasons.append(f"حجم غير طبيعي: {rvol:.1f}× المعتاد")
    elif rvol > 1.5:
        reasons.append(f"حجم أعلى من المعتاد: {rvol:.1f}×")
    if analysis.indicators.macd.histogram_rising:
        reasons.append("زخم MACD يتقوى")
    if nearest_distance_atr < 1.0:
        reasons.append("قريب جداً من مستوى فني مفصلي")
    if not reasons:
        reasons.append("لا توجد إشارات جاهزية بارزة اليوم")

    return round(score, 1), reasons
