from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.opportunities.schemas import MarketRegime
from app.providers.base import MarketDataAdapter


def current_session() -> str:
    now = datetime.now(ZoneInfo("America/New_York"))
    minutes = now.hour * 60 + now.minute
    if minutes < 570:
        return "pre_market"
    if minutes < 600:
        return "open"
    if minutes < 900:
        return "mid_session"
    if minutes <= 960:
        return "close"
    return "after_hours"


def classify_market(provider: MarketDataAdapter) -> tuple[MarketRegime, dict]:
    signals: dict[str, float | None] = {}
    positive = 0
    available = 0
    for symbol in ("SPY", "QQQ", "IWM"):
        try:
            frame = provider.get_daily_ohlcv(symbol, 40)
            close = frame["close"].astype(float)
            change = float((close.iloc[-1] / close.iloc[-6] - 1) * 100)
            signals[symbol] = round(change, 2)
            available += 1
            positive += change > 0
        except Exception:
            signals[symbol] = None
    if available < 2:
        return MarketRegime.HIGH_RISK, signals
    if positive == available:
        return MarketRegime.BULLISH, signals
    if positive == 0:
        return MarketRegime.BEARISH, signals
    if signals.get("IWM") is not None and signals["IWM"] < -2:
        return MarketRegime.HIGH_RISK, signals
    return MarketRegime.CHOPPY, signals
