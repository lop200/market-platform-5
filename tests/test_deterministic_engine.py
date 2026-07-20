"""End-to-end deterministic engine tests: smoke test + a frozen golden-file snapshot
(SRS 23 'Golden Files' — any unintended change in results must fail this test)."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from app.engines.deterministic.engine import run_deterministic_engine

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _frozen_synthetic_daily(n: int = 300, seed: int = 2026) -> pd.DataFrame:
    """Deterministic (seeded, not real-market) OHLCV so the golden snapshot is 100%
    reproducible — a fixed numpy seed produces the exact same sequence every run."""
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2025-01-01", periods=n, freq="B")
    close = pd.Series(100 + np.cumsum(rng.normal(0.05, 1.2, n)), index=dates)
    high = close + rng.uniform(0.2, 1.5, n)
    low = close - rng.uniform(0.2, 1.5, n)
    volume = pd.Series(rng.integers(1_000_000, 6_000_000, n), index=dates)
    return pd.DataFrame({"open": close, "high": high, "low": low, "close": close, "volume": volume})


def test_run_deterministic_engine_matches_frozen_golden_snapshot():
    daily = _frozen_synthetic_daily()
    result = run_deterministic_engine("GOLDNSYM", daily, data_quality="daily_only")

    actual = result.model_dump(mode="json")
    actual.pop("as_of")  # wall-clock timestamp, not part of the frozen snapshot

    expected = json.loads((FIXTURES_DIR / "golden_deterministic_analysis.json").read_text(encoding="utf-8"))
    assert actual == expected


def test_run_deterministic_engine_smoke_with_intraday_and_quote():
    from app.providers.base import Quote

    daily = _frozen_synthetic_daily(n=260, seed=99)
    intraday_index = pd.date_range("2026-01-05 09:30", periods=10, freq="min")
    intraday = pd.DataFrame(
        {
            "open": np.linspace(150, 152, 10),
            "high": np.linspace(150.5, 152.5, 10),
            "low": np.linspace(149.5, 151.5, 10),
            "close": np.linspace(150, 152, 10),
            "volume": [50_000] * 10,
        },
        index=intraday_index,
    )
    quote = Quote(symbol="TEST", price=152.0, bid=151.9, ask=152.1, volume=1000, as_of="2026-01-05T09:40:00Z", is_delayed=False)

    result = run_deterministic_engine("TEST", daily, intraday=intraday, quote=quote, data_quality="intraday")

    assert result.symbol == "TEST"
    assert result.data_quality == "intraday"
    assert result.indicators.vwap is not None
    assert result.liquidity.spread_pct is not None
    assert result.regime.label in {"trending_up", "trending_down", "ranging", "high_vol"}
    assert 0 <= result.scores.overall_confidence <= 100
