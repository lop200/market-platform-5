from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import numpy as np
import pandas as pd

from app.services.dashboard import _freshness, _technical_card


NOW = datetime(2026, 7, 21, 14, 30, 30, tzinfo=timezone.utc)


def _frame(rising: bool = True) -> pd.DataFrame:
    values = np.linspace(100, 130, 90)
    if not rising:
        values = values[::-1]
    index = pd.date_range("2026-04-22", periods=90, freq="D")
    close = pd.Series(values, index=index)
    return pd.DataFrame(
        {
            "open": close,
            "high": close + 1,
            "low": close - 1,
            "close": close,
            "volume": np.full(90, 2_000_000),
        },
        index=index,
    )


def test_dashboard_card_exposes_full_deterministic_reading_and_freshness():
    quote = SimpleNamespace(
        price=131.0, as_of="2026-07-21T14:30:00Z", is_delayed=False
    )

    card = _technical_card("NVDA", _frame(True), quote, now=NOW)

    assert card["symbol"] == "NVDA"
    assert card["price"] == 131.0
    assert card["trend"] == "صاعد"
    assert card["signal_class"] == "positive"
    assert card["above_ema20"] is True
    assert card["rsi"] > 50
    assert card["macd_histogram"] > 0
    assert card["ema20"] > 0
    assert card["support"] < card["price"]
    assert card["resistance"] >= card["price"]
    assert card["is_live"] is True
    assert card["freshness"] == "live"
    assert card["quote_age_seconds"] == 30
    assert len(card["sparkline"]) == 30


def test_stale_quote_is_never_labeled_live():
    quote = SimpleNamespace(
        price=131.0,
        as_of=(NOW - timedelta(minutes=10)).isoformat(),
        is_delayed=False,
    )

    card = _technical_card("NVDA", _frame(True), quote, now=NOW)

    assert card["is_live"] is False
    assert card["freshness"] == "stale"
    assert card["freshness_label"] == "قديم"


def test_delayed_or_closed_quotes_are_never_labeled_live():
    delayed = _freshness(
        age_seconds=10, delayed=True, market_open=True, provider="yfinance"
    )
    closed = _freshness(
        age_seconds=10, delayed=False, market_open=False, provider="alpaca"
    )

    assert delayed == {
        "freshness": "delayed",
        "freshness_label": "متأخر",
        "is_live": False,
    }
    assert closed == {
        "freshness": "closed",
        "freshness_label": "آخر سعر - السوق مغلق",
        "is_live": False,
    }
