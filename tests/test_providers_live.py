"""Limited live integration test (SRS 23 'Adapter Tests'). Not run by default (see
pyproject.toml addopts = "-m 'not live'") — opt in with `pytest -m live` when you want to
prove the yfinance dev-fallback actually reaches the network within a real M0 smoke test.
"""
from __future__ import annotations

import pytest

from app.providers.yfinance_provider import YFinanceProvider

pytestmark = pytest.mark.live


def test_yfinance_live_daily_ohlcv_smoke():
    provider = YFinanceProvider()
    df = provider.get_daily_ohlcv("AAPL", lookback_days=30)
    assert not df.empty
    assert {"open", "high", "low", "close", "volume"}.issubset(df.columns)
    assert provider.estimated_cost_per_call() == 0.0
