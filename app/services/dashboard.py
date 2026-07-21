"""Deterministic market dashboard composition with explicit data freshness."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx
import pandas as pd
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.core.cache import DBCacheAdapter
from app.core.cost_gate import CostGate
from app.engines.deterministic.indicators import compute_indicators, ema
from app.engines.deterministic.levels import detect_levels
from app.providers.factory import get_market_data_provider


MARKET_SYMBOLS = ("SPY", "QQQ", "DIA", "IWM")
WATCH_SYMBOLS = ("NVDA", "AAPL", "MSFT", "AMZN", "META", "TSLA", "AMD", "GOOGL")
LIVE_QUOTE_MAX_AGE_SECONDS = 60
STALE_QUOTE_AGE_SECONDS = 5 * 60
STALE_NEWS_AGE_SECONDS = 24 * 60 * 60


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_timestamp(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _age_seconds(value: str | datetime | None, now: datetime) -> int | None:
    parsed = _parse_timestamp(value)
    if parsed is None:
        return None
    return max(0, int((now - parsed).total_seconds()))


def _freshness(
    *, age_seconds: int | None, delayed: bool, market_open: bool | None, provider: str
) -> dict:
    live_eligible = provider == "alpaca" and market_open is True and not delayed
    is_live = bool(
        live_eligible
        and age_seconds is not None
        and age_seconds <= LIVE_QUOTE_MAX_AGE_SECONDS
    )
    if is_live:
        status, label = "live", "لحظي"
    elif market_open is False:
        status, label = "closed", "آخر سعر - السوق مغلق"
    elif delayed:
        status, label = "delayed", "متأخر"
    elif age_seconds is None or age_seconds > STALE_QUOTE_AGE_SECONDS:
        status, label = "stale", "قديم"
    else:
        status, label = "recent", "حديث - غير مصنف كلحظي"
    return {"freshness": status, "freshness_label": label, "is_live": is_live}


def _previous_close(frame: pd.DataFrame, now: datetime) -> float:
    close = frame["close"].astype(float)
    last_index = pd.Timestamp(frame.index[-1])
    if last_index.tzinfo is not None:
        last_date = last_index.tz_convert("UTC").date()
    else:
        last_date = last_index.date()
    position = -2 if last_date == now.date() and len(close) > 1 else -1
    return float(close.iloc[position])


def _trend_label(last: float, ema20: float, ema20_previous: float) -> tuple[str, str]:
    slope_pct = ((ema20 / ema20_previous) - 1) * 100 if ema20_previous else 0.0
    if last > ema20 and slope_pct > 0.15:
        return "صاعد", "positive"
    if last < ema20 and slope_pct < -0.15:
        return "هابط", "negative"
    return "عرضي", "neutral"


def _technical_card(
    symbol: str,
    frame: pd.DataFrame,
    quote: Any,
    *,
    provider: str = "alpaca",
    market_open: bool | None = True,
    now: datetime | None = None,
) -> dict:
    now = now or _utc_now()
    if frame.empty or len(frame) < 30:
        raise ValueError("at least 30 daily bars are required for dashboard analysis")

    close = frame["close"].astype(float)
    last = float(quote.price)
    previous = _previous_close(frame, now)
    change_pct = ((last / previous) - 1) * 100 if previous else 0.0
    indicators = compute_indicators(frame)
    ema20_series = ema(close, 20)
    ema20 = float(ema20_series.iloc[-1])
    ema20_previous = float(ema20_series.iloc[-6]) if len(ema20_series) >= 6 else ema20
    trend, signal_class = _trend_label(last, ema20, ema20_previous)

    levels = detect_levels(frame, indicators.atr_14, last)
    supports = sorted((level.price for level in levels.supports if level.price < last), reverse=True)
    resistances = sorted(level.price for level in levels.resistances if level.price > last)
    level_method = "swing-cluster"
    support = supports[0] if supports else float(frame["low"].tail(20).min())
    resistance = resistances[0] if resistances else float(frame["high"].tail(20).max())
    if not supports or not resistances:
        level_method = "20d-range-fallback"

    quote_age = _age_seconds(quote.as_of, now)
    freshness = _freshness(
        age_seconds=quote_age,
        delayed=bool(quote.is_delayed),
        market_open=market_open,
        provider=provider,
    )
    rsi_value = float(indicators.rsi_14)
    if rsi_value >= 70:
        rsi_label = "تشبع شرائي"
    elif rsi_value <= 30:
        rsi_label = "تشبع بيعي"
    else:
        rsi_label = "متوازن"

    macd_histogram = float(indicators.macd.histogram)
    macd_label = "إيجابي" if macd_histogram > 0 else "سلبي" if macd_histogram < 0 else "محايد"
    signal = f"اتجاه {trend} - زخم {macd_label}"

    return {
        "symbol": symbol,
        "price": round(last, 2),
        "previous_close": round(previous, 2),
        "change_pct": round(change_pct, 2),
        "rsi": round(rsi_value, 1),
        "rsi_label": rsi_label,
        "macd": round(float(indicators.macd.macd_line), 3),
        "macd_signal": round(float(indicators.macd.signal_line), 3),
        "macd_histogram": round(macd_histogram, 3),
        "macd_label": macd_label,
        "ema20": round(ema20, 2),
        "above_ema20": last >= ema20,
        "trend": trend,
        "signal": signal,
        "signal_class": signal_class,
        "support": round(support, 2),
        "resistance": round(resistance, 2),
        "support_distance_pct": round(((last / support) - 1) * 100, 2) if support else None,
        "resistance_distance_pct": round(((resistance / last) - 1) * 100, 2) if last else None,
        "level_method": level_method,
        "sparkline": [round(float(value), 2) for value in close.tail(30)],
        "as_of": quote.as_of,
        "quote_age_seconds": quote_age,
        "delayed": bool(quote.is_delayed),
        **freshness,
    }


def _feed_label(provider: str) -> str:
    return {
        "alpaca": "Alpaca IEX",
        "finnhub": "Finnhub",
        "yfinance": "Yahoo Finance - متأخر/غير رسمي",
    }.get(provider, provider)


def get_dashboard_snapshot(db: Session, settings: Settings | None = None) -> dict:
    settings = settings or get_settings()
    cache = DBCacheAdapter(db)
    cached = cache.get("dashboard:market:v3")
    if cached is not None:
        return {**cached, "cached": True, "served_at": _utc_now().isoformat()}

    provider = get_market_data_provider()
    gate = CostGate(db, settings)
    decision = gate.check_and_reserve(
        category="market_data",
        provider=f"{provider.provider_name}:dashboard",
        estimated_cost=provider.estimated_cost_per_call(),
    )
    if not decision.allowed:
        raise RuntimeError(decision.reason or "تعذّر السماح بتحديث بيانات السوق")

    try:
        market_open: bool | None = provider.is_market_open()
    except Exception:
        market_open = None

    now = _utc_now()
    cards: list[dict] = []
    errors: list[str] = []
    for symbol in (*MARKET_SYMBOLS, *WATCH_SYMBOLS):
        try:
            daily = provider.get_daily_ohlcv(symbol, 90)
            quote = provider.get_quote(symbol)
            cards.append(
                _technical_card(
                    symbol,
                    daily,
                    quote,
                    provider=provider.provider_name,
                    market_open=market_open,
                    now=now,
                )
            )
        except Exception as exc:
            errors.append(f"{symbol}: {type(exc).__name__}")

    if decision.ledger_id is not None:
        gate.record_actual(decision.ledger_id, 0.0)

    by_symbol = {card["symbol"]: card for card in cards}
    result = {
        "market_open": market_open,
        "provider": provider.provider_name,
        "feed": _feed_label(provider.provider_name),
        "updated_at": now.isoformat(),
        "served_at": now.isoformat(),
        "indices": [by_symbol[s] for s in MARKET_SYMBOLS if s in by_symbol],
        "watchlist": [by_symbol[s] for s in WATCH_SYMBOLS if s in by_symbol],
        "live_count": sum(1 for card in cards if card["is_live"]),
        "stale_count": sum(1 for card in cards if card["freshness"] == "stale"),
        "errors": errors,
        "partial": bool(errors),
        "cached": False,
        "poll_after_seconds": 30 if market_open else 120,
    }
    ttl = 30 if market_open else 120
    cache.set("dashboard:market:v3", result, ttl_seconds=ttl)
    return result


def get_market_news(db: Session, settings: Settings | None = None) -> dict:
    settings = settings or get_settings()
    cache = DBCacheAdapter(db)
    cached = cache.get("dashboard:news:v2")
    if cached is not None:
        return {**cached, "cached": True, "served_at": _utc_now().isoformat()}
    if not settings.alpaca_api_key or not settings.alpaca_api_secret:
        return {
            "items": [],
            "error": "مفاتيح Alpaca غير مضبوطة للأخبار",
            "updated_at": _utc_now().isoformat(),
            "cached": False,
        }

    gate = CostGate(db, settings)
    decision = gate.check_and_reserve(
        category="market_data", provider="alpaca:news", estimated_cost=0.0
    )
    if not decision.allowed:
        return {"items": [], "error": decision.reason, "cached": False}

    headers = {
        "APCA-API-KEY-ID": settings.alpaca_api_key,
        "APCA-API-SECRET-KEY": settings.alpaca_api_secret,
    }
    with httpx.Client(timeout=12.0) as client:
        response = client.get(
            "https://data.alpaca.markets/v1beta1/news",
            headers=headers,
            params={"limit": 18, "sort": "desc", "include_content": "false"},
        )
        response.raise_for_status()
        payload = response.json()

    now = _utc_now()
    items = []
    for article in payload.get("news", []):
        created_at = article.get("created_at")
        age = _age_seconds(created_at, now)
        items.append(
            {
                "id": article.get("id"),
                "headline": article.get("headline") or "خبر بلا عنوان",
                "summary": article.get("summary") or "",
                "source": article.get("source") or article.get("author") or "Alpaca News",
                "created_at": created_at,
                "age_seconds": age,
                "is_stale": age is None or age > STALE_NEWS_AGE_SECONDS,
                "url": article.get("url"),
                "symbols": (article.get("symbols") or [])[:5],
            }
        )
    if decision.ledger_id is not None:
        gate.record_actual(decision.ledger_id, 0.0)
    result = {
        "items": items,
        "updated_at": now.isoformat(),
        "served_at": now.isoformat(),
        "source": "Alpaca News",
        "cached": False,
    }
    cache.set("dashboard:news:v2", result, ttl_seconds=60)
    return result
