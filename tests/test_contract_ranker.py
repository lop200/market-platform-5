from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import pandas as pd
import pytest

from app.engines.options.contract_ranker import (
    fetch_alpaca_chain_calls,
    get_alpaca_expirations,
    rank_best_call_contract,
    rank_best_contract,
)


def _expiries(as_of: date, dtes: list[int]) -> list[str]:
    return [(as_of + timedelta(days=d)).isoformat() for d in dtes]


def _chain_df(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows)


def test_only_evaluates_expiries_within_zero_to_two_days():
    as_of = date(2026, 1, 1)
    expirations = _expiries(as_of, [2, 10, 27, 60])
    seen_expiries = []

    def fetch(expiry_str):
        seen_expiries.append(expiry_str)
        return _chain_df(
            [{"contractSymbol": "X", "strike": 100.0, "bid": 0.74, "ask": 0.78, "lastPrice": 0.76,
              "openInterest": 600, "volume": 300, "impliedVolatility": 0.35}]
        )

    result = rank_best_call_contract(expirations, fetch, underlying_price=100.0, atr_pct_relative=1.0,
                                      risk_free_rate=0.045, as_of=as_of)
    assert result is not None
    assert seen_expiries == [_expiries(as_of, [2])[0]]


def test_never_falls_back_to_a_longer_expiry():
    as_of = date(2026, 1, 1)
    expirations = _expiries(as_of, [5, 10])

    def fetch(expiry_str):
        return _chain_df(
            [{"contractSymbol": "X", "strike": 100.0, "bid": 0.74, "ask": 0.78, "lastPrice": 0.76,
              "openInterest": 600, "volume": 300, "impliedVolatility": 0.35}]
        )

    result = rank_best_call_contract(expirations, fetch, underlying_price=100.0, atr_pct_relative=1.0,
                                      risk_free_rate=0.045, as_of=as_of)
    assert result is None


def test_prefers_liquid_tight_spread_balanced_delta_contract():
    as_of = date(2026, 1, 1)
    expirations = _expiries(as_of, [2])

    def fetch(expiry_str):
        return _chain_df([
            # far OTM, illiquid, wide spread -> low score
            {"contractSymbol": "FAROTM", "strike": 150.0, "bid": 0.05, "ask": 0.30, "lastPrice": 0.1,
             "openInterest": 2, "volume": 1, "impliedVolatility": 0.6},
            # near-ATM, liquid, tight spread -> high score
            {"contractSymbol": "GOOD", "strike": 100.0, "bid": 0.74, "ask": 0.78, "lastPrice": 0.76,
             "openInterest": 800, "volume": 400, "impliedVolatility": 0.35},
        ])

    result = rank_best_call_contract(expirations, fetch, underlying_price=100.0, atr_pct_relative=1.0,
                                      risk_free_rate=0.045, as_of=as_of)
    assert result is not None
    assert result.contract_symbol == "GOOD"


def test_skips_contracts_with_missing_or_absurd_iv():
    as_of = date(2026, 1, 1)
    expirations = _expiries(as_of, [2])

    def fetch(expiry_str):
        return _chain_df([
            {"contractSymbol": "NANIV", "strike": 100.0, "bid": 0.74, "ask": 0.78, "lastPrice": 0.76,
             "openInterest": 800, "volume": 400, "impliedVolatility": float("nan")},
            {"contractSymbol": "HUGEIV", "strike": 100.0, "bid": 0.74, "ask": 0.78, "lastPrice": 0.76,
             "openInterest": 800, "volume": 400, "impliedVolatility": 5.0},
            {"contractSymbol": "OK", "strike": 100.0, "bid": 0.74, "ask": 0.78, "lastPrice": 0.76,
             "openInterest": 800, "volume": 400, "impliedVolatility": 0.35},
        ])

    result = rank_best_call_contract(expirations, fetch, underlying_price=100.0, atr_pct_relative=1.0,
                                      risk_free_rate=0.045, as_of=as_of)
    assert result is not None
    assert result.contract_symbol == "OK"


def test_returns_none_when_no_future_expirations():
    as_of = date(2026, 1, 1)
    result = rank_best_call_contract([], lambda e: None, underlying_price=100.0, atr_pct_relative=1.0,
                                      risk_free_rate=0.045, as_of=as_of)
    assert result is None


def test_returns_none_when_all_contracts_are_illiquid_or_bad_spread():
    as_of = date(2026, 1, 1)
    expirations = _expiries(as_of, [2])

    def fetch(expiry_str):
        return _chain_df([
            {"contractSymbol": "NOBID", "strike": 100.0, "bid": 0.0, "ask": 5.0, "lastPrice": 4.95,
             "openInterest": 800, "volume": 400, "impliedVolatility": 0.3},
        ])

    result = rank_best_call_contract(expirations, fetch, underlying_price=100.0, atr_pct_relative=1.0,
                                      risk_free_rate=0.045, as_of=as_of)
    assert result is None


def test_rejects_contract_above_maximum_premium():
    as_of = date(2026, 1, 1)
    chain = _chain_df([
        {"contractSymbol": "EXPENSIVE", "strike": 100.0, "bid": 1.01, "ask": 1.05, "lastPrice": 1.03,
         "openInterest": 800, "volume": 400, "impliedVolatility": 0.35},
    ])
    result = rank_best_call_contract(
        _expiries(as_of, [2]), lambda _: chain, 100.0, 1.0, 0.045, as_of
    )
    assert result is None


def test_rejects_wide_spread_and_low_open_interest_before_ranking():
    as_of = date(2026, 1, 1)
    chain = _chain_df([
        {"contractSymbol": "WIDE", "strike": 100.0, "bid": 0.50, "ask": 0.70, "lastPrice": 0.60,
         "openInterest": 800, "volume": 400, "impliedVolatility": 0.35},
        {"contractSymbol": "LOWOI", "strike": 100.0, "bid": 0.74, "ask": 0.78, "lastPrice": 0.76,
         "openInterest": 99, "volume": 400, "impliedVolatility": 0.35},
    ])
    result = rank_best_call_contract(
        _expiries(as_of, [2]), lambda _: chain, 100.0, 1.0, 0.045, as_of
    )
    assert result is None


def test_put_contract_is_ranked_with_negative_delta():
    as_of = date(2026, 1, 1)
    chain = _chain_df([
        {"contractSymbol": "GOODPUT", "strike": 100.0, "bid": 0.74, "ask": 0.78, "lastPrice": 0.76,
         "openInterest": 800, "volume": 400, "impliedVolatility": 0.35},
    ])
    result = rank_best_contract(
        _expiries(as_of, [2]), lambda _: chain, 100.0, 1.0, 0.045, "put", as_of
    )
    assert result is not None
    assert result.option_type == "put"
    assert result.greeks.delta < 0


# --- Alpaca chain fetchers (owner-confirmed 2026-07-18, replacing the yfinance interim
# workaround) — the Alpaca SDK clients are faked so these stay network-free unit tests,
# same convention as the rest of this file. ---


@dataclass
class _FakeContract:
    symbol: str
    strike_price: float
    expiration_date: date
    open_interest: str | None


@dataclass
class _FakeQuote:
    bid_price: float | None
    ask_price: float | None


@dataclass
class _FakeTrade:
    price: float | None


@dataclass
class _FakeSnapshot:
    latest_quote: _FakeQuote | None
    latest_trade: _FakeTrade | None


class _FakeContractsResponse:
    def __init__(self, contracts):
        self.option_contracts = contracts


class _FakeTradingClient:
    def __init__(self, contracts):
        self._contracts = contracts

    def get_option_contracts(self, request):
        expiry = getattr(request, "expiration_date", None)
        matching = [c for c in self._contracts if expiry is None or c.expiration_date.isoformat() == expiry]
        return _FakeContractsResponse(matching)


class _FakeDataClient:
    def __init__(self, snapshots: dict):
        self._snapshots = snapshots

    def get_option_snapshot(self, request):
        return self._snapshots


@pytest.fixture
def _fake_alpaca(monkeypatch):
    expiry = date(2026, 8, 15)
    contracts = [
        _FakeContract(symbol="GOOD", strike_price=101.0, expiration_date=expiry, open_interest="800"),
        _FakeContract(symbol="NOQUOTE", strike_price=105.0, expiration_date=expiry, open_interest=None),
    ]
    snapshots = {
        "GOOD": _FakeSnapshot(latest_quote=_FakeQuote(bid_price=4.9, ask_price=5.0), latest_trade=_FakeTrade(price=4.95)),
        # no snapshot entry at all for NOQUOTE -> bid/ask/last all None, must not crash
    }

    trading_client = _FakeTradingClient(contracts)
    monkeypatch.setattr("app.engines.options.contract_ranker._alpaca_trading_client", lambda: trading_client)
    monkeypatch.setattr(
        "alpaca.data.historical.option.OptionHistoricalDataClient",
        lambda *a, **kw: _FakeDataClient(snapshots),
    )

    class _FakeSettings:
        alpaca_api_key = "fake-key"
        alpaca_api_secret = "fake-secret"

    monkeypatch.setattr("app.config.get_settings", lambda: _FakeSettings())
    return expiry, contracts, snapshots


def test_get_alpaca_expirations_returns_sorted_unique_dates(_fake_alpaca):
    expiry, _, _ = _fake_alpaca
    assert get_alpaca_expirations("AAPL") == [expiry.isoformat()]


def test_fetch_alpaca_chain_calls_builds_expected_columns(_fake_alpaca):
    expiry, _, _ = _fake_alpaca
    df = fetch_alpaca_chain_calls("AAPL", expiry.isoformat(), underlying_price=100.0, risk_free_rate=0.045)
    assert set(df["contractSymbol"]) == {"GOOD", "NOQUOTE"}

    good = df[df["contractSymbol"] == "GOOD"].iloc[0]
    assert good["strike"] == 101.0
    assert good["bid"] == 4.9 and good["ask"] == 5.0
    assert good["openInterest"] == 800
    assert good["volume"] == 0  # not exposed by Alpaca's indicative feed
    assert good["impliedVolatility"] is not None and good["impliedVolatility"] > 0

    missing = df[df["contractSymbol"] == "NOQUOTE"].iloc[0]
    assert pd.isna(missing["bid"]) and pd.isna(missing["ask"])
    assert missing["openInterest"] == 0  # None open_interest defaults to 0, not a crash
    assert pd.isna(missing["impliedVolatility"])  # no price to solve IV from


def test_fetch_alpaca_chain_calls_feeds_rank_best_call_contract(_fake_alpaca):
    """End-to-end: the DataFrame this function builds is actually consumable by the same
    pure ranking function the yfinance path uses — no Alpaca-specific branching needed
    downstream."""
    expiry, _, _ = _fake_alpaca
    result = rank_best_call_contract(
        [expiry.isoformat()],
        lambda expiry_str: fetch_alpaca_chain_calls("AAPL", expiry_str, underlying_price=100.0, risk_free_rate=0.045),
        underlying_price=100.0,
        atr_pct_relative=1.0,
        risk_free_rate=0.045,
        as_of=date(2026, 7, 18),
        max_dte=45,
        max_premium=10.0,
        max_theta_decay_pct=100.0,
    )
    assert result is not None
    assert result.contract_symbol == "GOOD"  # NOQUOTE has no price -> no IV -> skipped
