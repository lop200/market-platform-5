from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone

import pandas as pd

from app.config import Settings
from app.providers.base import Quote


def _utc(value) -> datetime | None:
    if value is None:
        return None
    parsed = value.to_pydatetime() if hasattr(value, "to_pydatetime") else datetime.fromisoformat(
        str(value).replace("Z", "+00:00")
    )
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class DataQualityGate:
    valid_for_plan: bool
    reasons: list[str]
    warnings: list[str]
    trade_age_seconds: int | None
    bid_age_seconds: int | None
    ask_age_seconds: int | None
    candle_age_seconds: int | None
    quote_candle_skew_seconds: int | None
    market_open: bool

    def as_dict(self) -> dict:
        return asdict(self)


def evaluate_plan_data(
    quote: Quote | None,
    execution_bars: pd.DataFrame | None,
    settings: Settings,
    *,
    market_open: bool,
) -> DataQualityGate:
    reasons: list[str] = []
    warnings: list[str] = []
    trade_age = quote.trade_age_seconds if quote else None
    bid_age = quote.bid_age_seconds if quote else None
    ask_age = quote.ask_age_seconds if quote else None

    if quote is None:
        reasons.append("السعر الحالي غير متوفر")
    else:
        if quote.bid is None or quote.bid <= 0:
            reasons.append("Bid غير متوفر أو غير صالح")
        if quote.ask is None or quote.ask <= 0:
            reasons.append("Ask غير متوفر أو غير صالح")
        if quote.bid and quote.ask and quote.ask < quote.bid:
            reasons.append("Ask أقل من Bid")
        if quote.spread_pct is None or quote.spread_pct > settings.max_spread_pct:
            reasons.append("السبريد أعلى من الحد المسموح")
        ages = [value for value in (trade_age, bid_age, ask_age) if value is not None]
        if not ages or max(ages) > settings.max_quote_age_seconds:
            reasons.append("السعر أو Bid/Ask أقدم من الحد المسموح")
        bid_time = _utc(quote.bid_as_of or quote.as_of)
        ask_time = _utc(quote.ask_as_of or quote.as_of)
        if bid_time and ask_time:
            skew = abs(int((bid_time - ask_time).total_seconds()))
            if skew > settings.max_quote_timestamp_skew_seconds:
                reasons.append("توقيت Bid وAsk غير متزامن")
        if quote.feed and quote.feed.lower() == "iex":
            warnings.append("البيانات من IEX وقد لا تمثل كامل السوق الأمريكي.")
        if quote.is_delayed:
            warnings.append("بيانات المزود متأخرة")

    candle_age = None
    quote_candle_skew = None
    if execution_bars is None or execution_bars.empty or len(execution_bars) < 20:
        reasons.append("شموع 5 دقائق غير مكتملة")
    else:
        candle_time = _utc(execution_bars.index[-1])
        if candle_time:
            candle_age = max(0, int((datetime.now(timezone.utc) - candle_time).total_seconds()))
            if market_open and candle_age > settings.max_candle_age_seconds:
                reasons.append("آخر شمعة أقدم من الحد المسموح")
            quote_time = _utc(quote.as_of) if quote else None
            if quote_time:
                quote_candle_skew = abs(int((quote_time - candle_time).total_seconds()))
                if market_open and quote_candle_skew > settings.max_quote_candle_skew_seconds:
                    reasons.append("السعر والشموع غير متزامنين")

    if not market_open:
        reasons.append("السوق مغلق؛ لا تُبنى خطة دخول مباشرة")

    return DataQualityGate(
        valid_for_plan=not reasons,
        reasons=list(dict.fromkeys(reasons)),
        warnings=list(dict.fromkeys(warnings)),
        trade_age_seconds=trade_age,
        bid_age_seconds=bid_age,
        ask_age_seconds=ask_age,
        candle_age_seconds=candle_age,
        quote_candle_skew_seconds=quote_candle_skew,
        market_open=market_open,
    )
