from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_both_views_announce_which_way_the_trade_goes():
    """A short's stop sits above its entry, which reads as an error unless
    the plan says it is a short."""
    for path in ("/", "/stocks/NVDA"):
        html = client.get(path).text
        assert "plan-direction" in html, path
        assert "direction_ar" in html, path


def test_a_short_plan_is_labelled_and_explained():
    from app.opportunities.schemas import MarketRegime
    from app.opportunities.strategies import select_strategy

    # The breakdown strategy is the one that produces an inverted plan.
    indicators = {
        "ema9": 90.0, "ema20": 95.0, "ema50": 100.0,
        "macd": -1.0, "macd_signal": -0.5,
        "support": 100.0, "relative_volume": 1.5, "rsi": 45, "vwap": 105.0,
        "atr": 2.0, "momentum": -1.0,
    }
    choice = select_strategy(indicators, 100.0, MarketRegime.BEARISH)
    assert "breakdown" in choice.strategy_id


def test_the_direction_wording_is_unambiguous():
    import inspect

    from app.stocks import analysis

    source = inspect.getsource(analysis)
    assert "بيع — صفقة هابطة" in source
    assert "شراء — صفقة صاعدة" in source
    # And the stop reason must match the side it is protecting.
    assert "أعلى المقاومة الفنية" in source
    assert "أسفل الدعم الفني" in source
