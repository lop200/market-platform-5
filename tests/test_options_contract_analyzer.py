from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from app.engines.options.contract_analyzer import analyze_option_contract
from app.engines.options.greeks import theoretical_price
from app.engines.options.schemas import ExtractedContract


def _dates(n: int) -> pd.DatetimeIndex:
    return pd.date_range("2025-01-01", periods=n, freq="B")


def _trend_daily(n: int = 300, start: float = 100.0, end: float = 150.0) -> pd.DataFrame:
    close = pd.Series(np.linspace(start, end, n), index=_dates(n))
    high = close + 1.0
    low = close - 1.0
    volume = pd.Series([2_000_000] * n, index=_dates(n))
    return pd.DataFrame({"open": close, "high": high, "low": low, "close": close, "volume": volume})


AS_OF = date(2026, 1, 1)
RISK_FREE_RATE = 0.045


def _make_extracted_contract(option_type: str, underlying_price: float, strike: float, sigma: float = 0.3) -> ExtractedContract:
    expiry = AS_OF + timedelta(days=90)
    t = 90 / 365
    price = theoretical_price(underlying_price, strike, t, sigma, option_type, RISK_FREE_RATE)
    return ExtractedContract(
        symbol="TEST",
        option_type=option_type,
        strike=strike,
        expiry=expiry,
        contract_price=round(price, 2),
        extraction_confidence="high",
        raw_notes=None,
    )


def test_analyze_call_contract_end_to_end():
    daily = _trend_daily()
    underlying_price = float(daily["close"].iloc[-1])  # 150.0
    extracted = _make_extracted_contract("call", underlying_price, strike=140.0)

    result = analyze_option_contract(extracted, daily, RISK_FREE_RATE, vision_cost_usd=0.004, as_of=AS_OF)

    assert result.symbol == "TEST"
    assert result.option_type == "call"
    assert 0 < result.implied_volatility < 2.0
    assert 0 <= result.greeks.delta <= 1.0  # call delta is bounded [0, 1]
    assert result.greeks.gamma > 0
    assert result.greeks.theta < 0  # long options decay
    assert result.expected_move.upper_bound > result.underlying_price > result.expected_move.lower_bound
    assert result.cost_usd == pytest.approx(0.004)
    assert "TEST" in result.report_text_ar
    assert result.disclaimer in result.report_text_ar


def test_analyze_put_contract_delta_is_negative():
    daily = _trend_daily()
    underlying_price = float(daily["close"].iloc[-1])
    extracted = _make_extracted_contract("put", underlying_price, strike=160.0)

    result = analyze_option_contract(extracted, daily, RISK_FREE_RATE, vision_cost_usd=0.004, as_of=AS_OF)
    assert -1.0 <= result.greeks.delta <= 0


def test_analyze_contract_translates_levels_when_available():
    # A pure monotonic ramp has no local swing points at all (no supports/resistances),
    # so use a zigzag with genuine pullbacks (same style as the M1 levels tests).
    pattern = [100, 110, 120, 125, 128, 130, 125, 118, 110, 118, 128, 138, 145, 138, 128, 120, 125, 130]
    closes = (pattern * 7)[:120]
    idx = _dates(len(closes))
    daily = pd.DataFrame(
        {"open": closes, "high": closes, "low": closes, "close": closes, "volume": [2_000_000] * len(closes)},
        index=idx,
    )
    underlying_price = float(daily["close"].iloc[-1])
    extracted = _make_extracted_contract("call", underlying_price, strike=underlying_price - 10)

    result = analyze_option_contract(extracted, daily, RISK_FREE_RATE, vision_cost_usd=0.0, as_of=AS_OF)
    assert len(result.translated_levels) >= 1
    for level in result.translated_levels:
        assert level.estimated_contract_price >= 0


def test_analyze_contract_low_confidence_adds_warning_note():
    daily = _trend_daily()
    underlying_price = float(daily["close"].iloc[-1])
    extracted = _make_extracted_contract("call", underlying_price, strike=140.0)
    extracted = extracted.model_copy(update={"extraction_confidence": "low", "raw_notes": "تاريخ الانتهاء غير واضح"})

    result = analyze_option_contract(extracted, daily, RISK_FREE_RATE, vision_cost_usd=0.0, as_of=AS_OF)
    assert "ثقة منخفضة" in result.report_text_ar
    assert "تاريخ الانتهاء غير واضح" in result.report_text_ar


def test_analyze_contract_report_has_no_banned_phrases():
    from app.legal.disclaimers import BANNED_PHRASES_AR

    daily = _trend_daily()
    underlying_price = float(daily["close"].iloc[-1])
    extracted = _make_extracted_contract("call", underlying_price, strike=140.0)
    result = analyze_option_contract(extracted, daily, RISK_FREE_RATE, vision_cost_usd=0.0, as_of=AS_OF)
    for phrase in BANNED_PHRASES_AR:
        assert phrase not in result.report_text_ar
