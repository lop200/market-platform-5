from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
import uuid

import pandas as pd
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import app
from app.providers.base import MarketDataAdapter, Quote
from app.providers.resilient import ResilientMarketDataProvider
from app.stocks.analysis import analyze_single_stock


class SingleSymbolProvider(MarketDataAdapter):
    provider_name = "fake"

    def __init__(self):
        self.requested: list[str] = []

    def get_quote(self, symbol: str) -> Quote:
        self.requested.append(symbol)
        return Quote(
            symbol=symbol,
            price=130.0,
            bid=129.9,
            ask=130.1,
            volume=2_000_000,
            as_of=datetime.now(timezone.utc).isoformat(),
            is_delayed=False,
            provider="fake",
            feed="sip",
        )

    def get_daily_ohlcv(self, symbol: str, lookback_days: int) -> pd.DataFrame:
        self.requested.append(symbol)
        count = max(240, lookback_days)
        return pd.DataFrame({
            "open": [110 + index * .08 for index in range(count)],
            "high": [111 + index * .08 for index in range(count)],
            "low": [109 + index * .08 for index in range(count)],
            "close": [110.5 + index * .08 for index in range(count)],
            "volume": [3_000_000] * count,
        })

    def get_intraday(self, symbol: str, interval: str) -> pd.DataFrame:
        self.requested.append(symbol)
        count = 80
        index = pd.date_range(
            end=datetime.now(timezone.utc), periods=count,
            freq={"1m": "1min", "5m": "5min", "15m": "15min", "1h": "1h"}.get(interval, "5min"),
        )
        return pd.DataFrame({
            "open": [125 + item * .05 for item in range(count)],
            "high": [125.3 + item * .05 for item in range(count)],
            "low": [124.8 + item * .05 for item in range(count)],
            "close": [125.1 + item * .05 for item in range(count)],
            "volume": [80_000] * count,
        }, index=index)

    def get_company_profile(self, symbol: str) -> dict:
        self.requested.append(symbol)
        return {"name": "NVIDIA", "market_cap": 3_000_000_000_000, "float_shares": 24_000_000_000}

    def list_active_us_symbols(self, limit: int = 1000) -> list[str]:
        raise AssertionError("single-symbol analysis must not load the scanner universe")

    def estimated_cost_per_call(self) -> float:
        return 0

    def is_market_open(self) -> bool:
        return True


def test_nvda_analysis_ignores_scanner_price_range_and_remains_complete(db_session, monkeypatch):
    monkeypatch.setattr("app.stocks.analysis.intraday_expected_move", lambda *args, **kwargs: (10.0, 55.0))
    provider = SingleSymbolProvider()
    result = analyze_single_stock(
        db_session,
        provider,
        Settings(news_provider="none", price_verification_enabled=False),
        "NVDA",
    )
    assert result["symbol"] == "NVDA"
    assert result["quote"]["price"] == 130
    assert result["company_name"] == "NVIDIA"
    assert result["status"] in {"conditional_entry", "no_trade"}
    assert result["trade_plan"]["entry_from"] is not None
    assert result["trade_plan"]["stop"] is not None
    assert len(result["trade_plan"]["targets"]) >= 2
    assert "NVDA" in provider.requested
    assert set(provider.requested) <= {"NVDA", "SPY", "QQQ", "IWM"}


def test_single_symbol_result_has_charts_safe_probabilities_and_time_window(db_session, monkeypatch):
    monkeypatch.setattr("app.stocks.analysis.intraday_expected_move", lambda *args, **kwargs: (10.0, 55.0))
    result = analyze_single_stock(
        db_session, SingleSymbolProvider(), Settings(news_provider="none", price_verification_enabled=False), "NVDA"
    )
    assert all(result["charts"][frame] for frame in ("1m", "5m", "15m", "1h", "1d"))
    assert "ليس ضمانًا" in result["probability_disclaimer"]
    assert ":" not in result["time_estimate"]["expected"]
    assert "اتجاه السهم فقط" in result["directional_bias"]["warning"]
    serialized = str(result).lower()
    assert "greeks" not in serialized
    assert "contract_price" not in serialized


def test_stock_page_renders_even_before_or_without_an_opportunity():
    response = TestClient(app).get("/stocks/NVDA")
    assert response.status_code == 200
    assert 'dir="rtl"' in response.text
    assert "لا يطبق فلتر سعر الماسح" in response.text
    assert '<div id="chart">' in response.text
    assert "lightweight-charts" in response.text
    assert 'id="decision"' in response.text
    assert 'id="opportunityScores"' in response.text
    assert "Final Confidence" in response.text
    assert "احتمال نجاح الصفقة" in response.text
    assert '?refresh=true' in response.text
    assert "[1000,2000,3000]" in response.text
    assert "document.hidden" in response.text
    assert 'window.addEventListener("pagehide"' in response.text
    assert "@media(min-width:760px)" in response.text


def test_single_symbol_route_never_starts_market_scan(monkeypatch):
    from app.api import routes_opportunities

    calls = []
    monkeypatch.setattr(
        routes_opportunities,
        "create_symbol_analysis",
        lambda symbol, **kwargs: calls.append((symbol, kwargs)) or SimpleNamespace(id=uuid.uuid4(), status="queued"),
    )
    monkeypatch.setattr(
        routes_opportunities,
        "create_scan",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("market scan must not start")),
    )
    response = TestClient(app).post("/api/v1/opportunities/symbols/NVDA")
    assert response.status_code == 200
    assert calls == [("NVDA", {"refresh": False})]


def test_manual_symbol_refresh_bypasses_symbol_cache(monkeypatch):
    from app.api import routes_opportunities

    calls = []
    monkeypatch.setattr(
        routes_opportunities,
        "create_symbol_analysis",
        lambda symbol, **kwargs: calls.append((symbol, kwargs))
        or SimpleNamespace(id=uuid.uuid4(), status="queued"),
    )
    response = TestClient(app).post("/api/v1/opportunities/symbols/QQQ?refresh=true")
    assert response.status_code == 200
    assert calls == [("QQQ", {"refresh": True})]


def test_market_scan_price_filter_is_optional(monkeypatch):
    from app.api import routes_opportunities

    captured = []
    monkeypatch.setattr(
        routes_opportunities,
        "create_scan",
        lambda **kwargs: captured.append(kwargs) or SimpleNamespace(id=uuid.uuid4(), status="queued"),
    )
    client = TestClient(app)
    assert client.post("/api/v1/opportunities/scans?all_prices=true&universe_limit=50").status_code == 200
    assert captured[-1] == {"min_price": None, "max_price": None, "universe_limit": 50}
    assert client.post("/api/v1/opportunities/scans?all_prices=false&min_price=2&max_price=10").status_code == 200
    assert captured[-1]["min_price"] == 2
    assert captured[-1]["max_price"] == 10
    assert client.post("/api/v1/opportunities/scans").status_code == 200
    assert captured[-1]["min_price"] is None
    assert captured[-1]["max_price"] is None


class BatchProvider(SingleSymbolProvider):
    def __init__(self):
        super().__init__()
        self.batch_calls = 0

    @property
    def supports_batch_daily_ohlcv(self) -> bool:
        return True

    def get_daily_ohlcv_many(self, symbols, lookback_days):
        self.batch_calls += 1
        return {symbol: self.get_daily_ohlcv(symbol, lookback_days) for symbol in symbols}


def test_batch_results_are_cached_and_count_actual_api_requests():
    inner = BatchProvider()
    provider = ResilientMarketDataProvider(
        inner,
        Settings(cache_ttl_ohlcv_daily_seconds=60, external_max_retries=0),
    )
    provider.get_daily_ohlcv_many(["NVDA", "AAPL"], 40)
    provider.get_daily_ohlcv_many(["NVDA", "AAPL"], 40)
    telemetry = provider.telemetry_snapshot()
    assert inner.batch_calls == 1
    assert telemetry["api_requests"] == 1
    assert telemetry["cache_hits"] == 1
