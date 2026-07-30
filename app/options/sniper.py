from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

from app.config import Settings
from app.options.schemas import RankedOptionContract, RawOptionContract

NEW_YORK = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class SniperUniverse:
    contracts: list[RawOptionContract]
    stage: str
    stage_label_ar: str
    allowed_strikes: tuple[float, ...]
    atm_strike: float | None


class ShortDTEOptionSniper:
    """Deterministic short-DTE universe and display-mode selector."""

    def __init__(self, settings: Settings):
        self.settings = settings

    @property
    def enabled(self) -> bool:
        return (
            self.settings.options_scalp_mode
            and self.settings.options_scalp_paper_only
            and self.settings.options_paper_only
        )

    def select_universe(
        self,
        contracts: list[RawOptionContract],
        *,
        underlying_price: float,
        now: datetime,
    ) -> SniperUniverse:
        if not self.enabled or not contracts:
            return SniperUniverse(contracts, "standard", "7–30 DTE", (), None)
        today = now.astimezone(NEW_YORK).date()
        live = [
            item
            for item in contracts
            if (item.expiration - today).days >= self.settings.options_scalp_min_dte
            and (item.expiration - today).days <= self.settings.options_max_dte
        ]
        if not live:
            return SniperUniverse(
                [], "unavailable", "لا توجد عقود ضمن النطاق", (), None
            )

        strikes = sorted(
            {float(item.strike) for item in live},
            key=lambda strike: (abs(strike - underlying_price), strike),
        )
        count = 1 + self.settings.options_scalp_max_strikes_from_atm * 2
        allowed = tuple(strikes[:count])
        narrowed = [item for item in live if float(item.strike) in allowed]
        return SniperUniverse(
            narrowed,
            "candidate",
            "0–2 DTE ثم الاحتياط",
            allowed,
            strikes[0] if strikes else None,
        )

    def strategy_name(self, stock_analysis: dict, now: datetime) -> str:
        eastern = now.astimezone(NEW_YORK)
        minutes_from_open = (
            eastern.hour * 60 + eastern.minute - (9 * 60 + 30)
        )
        if 0 <= minutes_from_open < 5:
            return "Opening Range Breakout"
        source = " ".join(
            str(value)
            for value in (
                (stock_analysis.get("strategy") or {}).get("name"),
                (stock_analysis.get("strategy") or {}).get("name_ar"),
                (stock_analysis.get("strategy") or {}).get("reason"),
            )
            if value
        ).casefold()
        mapping = (
            (("retest", "إعادة اختبار"), "Breakout Retest"),
            (("vwap", "استعادة"), "VWAP Reclaim"),
            (("rejection", "رفض"), "VWAP Rejection"),
            (("pullback", "تراجع"), "Pullback Continuation"),
            (("failed", "فشل"), "Failed Breakout Reversal"),
            (("range", "نطاق"), "Range Break"),
            (("breakout", "اختراق", "كسر"), "Opening Range Breakout"),
        )
        for tokens, name in mapping:
            if any(token in source for token in tokens):
                return name
        return "Momentum Continuation"

    @staticmethod
    def time_remaining_minutes(contract: RawOptionContract, now: datetime) -> int:
        eastern = now.astimezone(NEW_YORK)
        settlement = datetime.combine(
            contract.expiration, time(16, 0), tzinfo=NEW_YORK
        )
        return max(0, int((settlement - eastern).total_seconds() // 60))

    @staticmethod
    def choose_modes(
        contracts: list[RankedOptionContract],
    ) -> tuple[list[RankedOptionContract], dict]:
        if not contracts:
            return [], {}
        best = max(
            contracts,
            key=lambda item: (
                item.suitability_score,
                -abs(item.distance_to_strike_pct),
                -item.spread_pct,
            ),
        )
        acceptable = [
            item
            for item in contracts
            if item.budget_fit
            and item.liquidity_score >= 40
            and abs(item.delta) >= 0.35
        ]
        cheapest = min(
            acceptable or contracts,
            key=lambda item: (item.contract_cost, -item.suitability_score),
        )
        higher_risk_pool = [
            item
            for item in acceptable
            if item.moneyness == "OTM" and item.symbol not in {best.symbol, cheapest.symbol}
        ]
        higher_risk = min(
            higher_risk_pool,
            key=lambda item: (item.contract_cost, -item.suitability_score),
        ) if higher_risk_pool else None
        ordered: list[RankedOptionContract] = []
        labels = (
            (best, "near_safe", "الأقرب والأكثر أمانًا"),
            (cheapest, "cheapest_acceptable", "الأرخص المقبول"),
            (higher_risk, "higher_risk", "مخاطرة أعلى"),
        )
        modes: dict[str, str | None] = {}
        for item, key, label in labels:
            modes[key] = item.symbol if item else None
            if item and item.symbol not in {row.symbol for row in ordered}:
                item.sniper_mode_ar = label
                ordered.append(item)
        return ordered[:3], modes
