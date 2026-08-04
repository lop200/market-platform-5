from datetime import datetime, timezone

from app.opportunities.scanner import _trace_candidate_score, _volume_metrics
from app.providers.base import Quote


def quote(*, price=100.0, volume=1_000_000):
    now = datetime.now(timezone.utc).isoformat()
    return Quote(
        symbol="TEST", price=price, bid=price - 0.01, ask=price + 0.01,
        volume=volume, as_of=now, is_delayed=False, provider="alpaca", feed="sip",
    )


def test_volume_and_dollar_volume_use_the_same_selected_source():
    volume, dollar, source = _volume_metrics(quote(price=25, volume=2_000_000), {})
    assert volume == 2_000_000
    assert dollar == 50_000_000
    assert source == "alpaca_snapshot_volume"
    volume, dollar, source = _volume_metrics(
        quote(price=25, volume=2_000_000), {"session_volume": 500_000}
    )
    assert volume == 500_000
    assert dollar == 12_500_000
    assert source == "intraday_session_bars"


def test_candidate_scores_change_with_liquidity_price_and_momentum_and_are_traceable():
    low = {
        "price": 2, "volume": 1_000, "dollar_volume": 2_000,
        "relative_volume": 0.2, "change_pct": 0.1, "data_state": "LIVE",
        "volume_source": "alpaca_snapshot_volume",
    }
    high = {
        "price": 200, "volume": 5_000_000, "dollar_volume": 1_000_000_000,
        "relative_volume": 2.5, "change_pct": 3.0, "data_state": "LIVE",
        "trend": "صاعد", "support": 195, "resistance": 205,
        "volume_source": "intraday_session_bars",
    }
    low_score, low_debug = _trace_candidate_score(low)
    high_score, high_debug = _trace_candidate_score(high)
    assert high_score > low_score
    assert high_debug["inputs"]["dollar_volume"] == 1_000_000_000
    assert high_debug["components"]["momentum"] > low_debug["components"]["momentum"]
    assert high_debug["total"] == high_score
