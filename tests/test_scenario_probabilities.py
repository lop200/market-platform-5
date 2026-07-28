from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.engines.deterministic.engine import run_deterministic_engine


def _trend(start: float, end: float) -> pd.DataFrame:
    index = pd.date_range("2025-01-01", periods=300, freq="B")
    close = np.linspace(start, end, len(index))
    return pd.DataFrame(
        {
            "open": close,
            "high": close + 1,
            "low": close - 1,
            "close": close,
            "volume": np.full(len(index), 2_000_000.0),
        },
        index=index,
    )


@pytest.mark.parametrize(
    ("start", "end", "dominant"),
    [(50.0, 150.0, "bullish_pct"), (150.0, 50.0, "bearish_pct")],
)
def test_scenario_probabilities_follow_clear_trend_and_sum_to_100(start, end, dominant):
    analysis = run_deterministic_engine("TEST", _trend(start, end))
    probabilities = analysis.scenario_probabilities
    assert probabilities is not None
    values = [
        probabilities.bullish_pct,
        probabilities.bearish_pct,
        probabilities.neutral_pct,
    ]
    assert sum(values) == pytest.approx(100.0)
    assert getattr(probabilities, dominant) == max(values)
    assert probabilities.calibrated is False


def test_missing_weekly_history_allocates_uncertainty_instead_of_forcing_direction():
    analysis = run_deterministic_engine("TEST", _trend(100.0, 101.0).head(20))
    probabilities = analysis.scenario_probabilities
    assert probabilities is not None
    assert probabilities.neutral_pct > 0
    assert (
        probabilities.bullish_pct
        + probabilities.bearish_pct
        + probabilities.neutral_pct
    ) == pytest.approx(100.0)
