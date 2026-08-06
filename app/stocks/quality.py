from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone

import pandas as pd

from app.config import Settings
from app.providers.base import Quote
from app.stocks.rules import allowed_stock_spread_pct


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
    price_age_seconds: int | None
    latest_bar_age_seconds: int | None
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
    price_age = quote.age_seconds if quote else None
    latest_bar_age = quote.bar_age_seconds if quote else None

    if quote is None:
        reasons.append("السعر الحالي غير متوفر")
    else:
        if quote.bid is None or quote.bid <= 0:
            reasons.append("Bid غير متوفر أو غير صالح")
        if quote.ask is None or quote.ask <= 0:
            reasons.append("Ask غير متوفر أو غير صالح")
        if quote.bid and quote.ask and quote.ask < quote.bid:
            reasons.append("Ask أقل من Bid")
        if quote.spread_pct is None or quote.spread_pct > allowed_stock_spread_pct(quote.price, settings):
            reasons.append("السبريد أعلى من الحد المسموح")
        if price_age is None or price_age > settings.max_quote_age_seconds:
            reasons.append("أحدث سعر من Trade/Quote/Bar أقدم من الحد المسموح")
        if trade_age is None or trade_age > settings.max_quote_age_seconds:
            reasons.append("آخر صفقة أقدم من الحد المسموح")
        if bid_age is None or ask_age is None or max(bid_age, ask_age) > settings.max_quote_age_seconds:
            reasons.append("Bid/Ask أقدم من الحد المسموح لبناء خطة")
        bid_time = _utc(quote.bid_as_of or quote.as_of)
        ask_time = _utc(quote.ask_as_of or quote.as_of)
        if bid_time and ask_time:
            skew = abs(int((bid_time - ask_time).total_seconds()))
            if skew > settings.max_quote_timestamp_skew_seconds:
                reasons.append("توقيت Bid وAsk غير متزامن")
        if quote.feed and quote.feed.lower() == "iex":
            warnings.append("البيانات من IEX وقد لا تمثل كامل السوق الأمريكي.")
            warnings.append("قد لا يسجل IEX صفقة أو شمعة في كل دقيقة خلال pre-market.")
        if quote.is_delayed:
            warnings.append("بيانات المزود متأخرة")
        references = [
            value for value in (quote.mid, quote.last_trade, quote.bar_close)
            if value is not None and value > 0
        ]
        if len(references) >= 2:
            divergence = (max(references) - min(references)) / min(references) * 100
            if divergence > settings.max_price_source_divergence_pct:
                reasons.append("Data Conflict — اختلاف داخلي بين Mid وآخر صفقة والشمعة")

    candle_age = None
    quote_candle_skew = None
    if execution_bars is None or execution_bars.empty or len(execution_bars) < 20:
        reasons.append("شموع 5 دقائق غير مكتملة")
    else:
        candle_time = _utc(execution_bars.index[-1])
        if candle_time:
            candle_age = max(0, int((datetime.now(timezone.utc) - candle_time).total_seconds()))
            fresh_realtime = price_age is not None and price_age <= settings.max_quote_age_seconds
            iex_sparse_bar = bool(quote and quote.feed and quote.feed.lower() == "iex" and fresh_realtime)
            # Five-minute bars are timestamped at the start of their bucket.
            # The separately fetched one-minute bar is the strict freshness
            # proof when that aggregate candle appears older than two minutes.
            realtime_bar_fresh = (
                latest_bar_age is not None
                and latest_bar_age <= settings.max_candle_age_seconds
            )
            realtime_proof = iex_sparse_bar or realtime_bar_fresh
            if market_open and candle_age > settings.max_candle_age_seconds and not realtime_proof:
                reasons.append("آخر شمعة أقدم من الحد المسموح")
            elif market_open and candle_age > settings.max_candle_age_seconds and realtime_proof:
                warnings.append("آخر شمعة قديمة، لكن Trade/Quote أحدث؛ استُخدم السعر الحديث.")
            quote_time = _utc(quote.as_of) if quote else None
            if quote_time:
                quote_candle_skew = abs(int((quote_time - candle_time).total_seconds()))
                if market_open and quote_candle_skew > settings.max_quote_candle_skew_seconds and not realtime_proof:
                    reasons.append("السعر والشموع غير متزامنين")

    if not market_open:
        warnings.append("القراءة خارج الجلسة الرسمية؛ تعتمد صلاحيتها على حداثة Trade وQuote والسبريد.")

    return DataQualityGate(
        valid_for_plan=not reasons,
        reasons=list(dict.fromkeys(reasons)),
        warnings=list(dict.fromkeys(warnings)),
        trade_age_seconds=trade_age,
        bid_age_seconds=bid_age,
        ask_age_seconds=ask_age,
        price_age_seconds=price_age,
        latest_bar_age_seconds=latest_bar_age,
        candle_age_seconds=candle_age,
        quote_candle_skew_seconds=quote_candle_skew,
        market_open=market_open,
    )
