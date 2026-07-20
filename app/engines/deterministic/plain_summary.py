"""Plain-language opening summary for the single-stock report — new presentation layer,
not in SRS section 13 (that section governs the LLM narrative; this is pure deterministic
Python, CLAUDE.md rule 1). The goal is a report that opens in everyday language before any
jargon (RSI/MACD/ADX move into a collapsed "تفاصيل فنية" section in the template).

Every field here is arithmetic on numbers the deterministic engine already produced —
no LLM, no new indicator.
"""
from __future__ import annotations

import pandas as pd
from pydantic import BaseModel

from app.engines.deterministic.schemas import DeterministicAnalysis

STATE_LABELS = {
    "trending_up": "قوي صاعد",
    "trending_down": "ضعيف هابط",
    "ranging": "متذبذب",
    "high_vol": "متذبذب",
}


class PlainSummary(BaseModel):
    open_price: float
    current_price: float
    change_from_open_pct: float
    state_label: str  # "قوي صاعد" / "متذبذب" / "ضعيف هابط"
    direction_text: str  # deterministic Arabic sentence about the nearest notable zones


def compute_open_price(daily: pd.DataFrame, intraday: pd.DataFrame | None) -> float:
    """Today's session open when intraday bars are available (most accurate while the
    market is live); otherwise the most recent daily bar's open (last completed session)."""
    if intraday is not None and len(intraday) > 0:
        return float(intraday["open"].iloc[0])
    return float(daily["open"].iloc[-1])


def _state_label(regime_label: str) -> str:
    return STATE_LABELS.get(regime_label, "متذبذب")


def _direction_text(analysis: DeterministicAnalysis) -> str:
    regime = analysis.regime.label
    if regime == "trending_down" and analysis.levels.supports:
        # nearest first, descending price -> "toward X, and if it breaks, toward Y"
        ordered = sorted(analysis.levels.supports, key=lambda lv: lv.price, reverse=True)
        zone1, zone2 = ordered[0].price, (ordered[1].price if len(ordered) > 1 else None)
        if zone2 is not None:
            return f"الاتجاه: السهم يتحرك نحو منطقة {zone1:.2f}، وإذا كسرها يتجه لمنطقة {zone2:.2f}."
        return f"الاتجاه: السهم يتحرك نحو منطقة {zone1:.2f}."

    if analysis.levels.resistances:
        ordered = sorted(analysis.levels.resistances, key=lambda lv: lv.price)
        zone1, zone2 = ordered[0].price, (ordered[1].price if len(ordered) > 1 else None)
        if zone2 is not None:
            return f"الاتجاه: السهم يتحرك نحو منطقة {zone1:.2f}، وإذا كسرها يتجه لمنطقة {zone2:.2f}."
        return f"الاتجاه: السهم يتحرك نحو منطقة {zone1:.2f}."

    if analysis.levels.supports:
        nearest_support = max(lv.price for lv in analysis.levels.supports)
        return f"الاتجاه: السهم يتحرك ضمن نطاق فوق منطقة {nearest_support:.2f}."

    return "الاتجاه: لا توجد مناطق فنية كافية حالياً لتحديد اتجاه واضح."


def build_plain_summary(analysis: DeterministicAnalysis, open_price: float) -> PlainSummary:
    change_from_open_pct = (analysis.last_close / open_price - 1) * 100 if open_price else 0.0
    return PlainSummary(
        open_price=round(open_price, 2),
        current_price=analysis.last_close,
        change_from_open_pct=round(change_from_open_pct, 2),
        state_label=_state_label(analysis.regime.label),
        direction_text=_direction_text(analysis),
    )
