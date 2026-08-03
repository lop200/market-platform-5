from __future__ import annotations

from app.opportunities.schemas import MarketRegime
from app.options.market_clock import market_session
from app.providers.base import MarketDataAdapter


def current_session() -> str:
    session = market_session()
    if session.code != "regular":
        return session.code
    minutes = session.new_york_time.hour * 60 + session.new_york_time.minute
    if minutes < 600:
        return "open"
    if minutes < 900:
        return "mid_session"
    return "close"


def classify_market(provider: MarketDataAdapter) -> tuple[MarketRegime, dict]:
    signals: dict[str, float | None] = {}
    positive = 0
    available = 0
    symbols = ("SPY", "QQQ", "IWM")
    frames = (
        provider.get_daily_ohlcv_many(list(symbols), 40)
        if provider.supports_batch_daily_ohlcv else {}
    )
    for symbol in symbols:
        try:
            frame = frames.get(symbol) if frames else provider.get_daily_ohlcv(symbol, 40)
            close = frame["close"].astype(float)
            change = float((close.iloc[-1] / close.iloc[-6] - 1) * 100)
            signals[symbol] = round(change, 2)
            available += 1
            positive += change > 0
        except Exception:
            signals[symbol] = None
    if available < 2:
        signals["nasdaq_direction"] = "unknown"
        return MarketRegime.HIGH_RISK, signals
    qqq_change = signals.get("QQQ")
    signals["nasdaq_direction"] = (
        "bullish" if qqq_change is not None and qqq_change > 0
        else "bearish" if qqq_change is not None and qqq_change < 0
        else "neutral"
    )
    if positive == available:
        return MarketRegime.BULLISH, signals
    if positive == 0:
        return MarketRegime.BEARISH, signals
    if signals.get("IWM") is not None and signals["IWM"] < -2:
        return MarketRegime.HIGH_RISK, signals
    return MarketRegime.CHOPPY, signals
