"""Self-audit tests with synthetic price paths with KNOWN outcomes (SRS 23)."""
from __future__ import annotations

from datetime import date, datetime, timezone

import pandas as pd
import pytest

from app.audit.self_audit import evaluate_snipe_targets, evaluate_targets, run_self_audit_once
from app.db import repository
from app.db.models import AccuracyStats, AuditResult
from app.engines.deterministic.schemas import LevelStrength, Levels
from app.providers.base import MarketDataAdapter, Quote


class FakeMarketDataAdapter(MarketDataAdapter):
    def __init__(self, daily: pd.DataFrame):
        self._daily = daily

    def get_daily_ohlcv(self, symbol, lookback_days):
        return self._daily

    def get_intraday(self, symbol, interval):
        return None

    def get_quote(self, symbol):
        return Quote(symbol=symbol, price=100.0, bid=None, ask=None, volume=None, as_of="2026-01-01T00:00:00Z", is_delayed=True)

    def estimated_cost_per_call(self):
        return 0.0

    def is_market_open(self):
        return True

    @property
    def provider_name(self):
        return "fake"


def _make_completed_analysis(
    db, symbol="TEST", regime="trending_up", requested_at=None, scenario="bullish",
    support=95.0, resistance=105.0, invalidation=93.0,
):
    requested_at = requested_at or datetime(2026, 1, 5, tzinfo=timezone.utc)
    record = repository.create_analysis(
        db, symbol=symbol, market_open=True, data_provider="fake",
        deterministic_json={}, scores={"technical": 50, "volatility": 50, "liquidity": 50, "risk": 50, "overall_confidence": 50},
        regime=regime, status="data_only",
    )
    record.requested_at = requested_at
    db.commit()
    repository.update_analysis_report(
        db, record.id, report_text_ar="x", llm_provider="fake",
        llm_input_tokens=1, llm_output_tokens=1, total_cost_usd=0.01, status="completed",
    )
    levels = Levels(
        supports=[LevelStrength(price=support, touches=1, last_touch_bars_ago=1, avg_volume_at_touches=1, strength_score=1)],
        resistances=[LevelStrength(price=resistance, touches=1, last_touch_bars_ago=1, avg_volume_at_touches=1, strength_score=1)],
        invalidation=invalidation,
    )
    repository.create_audit_targets(db, record.id, symbol, price_at_analysis=100.0, levels=levels, primary_scenario=scenario)
    db.refresh(record)
    return record


def _synthetic_daily(bar_values: dict[str, tuple[float, float, float]], start="2025-12-01", periods=90) -> pd.DataFrame:
    """bar_values: date-string -> (open/close, high, low) overrides; everything else flat at 100."""
    idx = pd.bdate_range(start=start, periods=periods)
    opens = [100.0] * periods
    highs = [101.0] * periods
    lows = [99.0] * periods
    closes = [100.0] * periods
    for date_str, (c, h, low) in bar_values.items():
        pos = idx.get_loc(pd.Timestamp(date_str))
        opens[pos] = c
        closes[pos] = c
        highs[pos] = h
        lows[pos] = low
    return pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": [1_000_000] * periods}, index=idx
    )


# --- evaluate_targets rubric unit tests ---

def _mk_targets(scenario="bullish", support=95.0, resistance=105.0, invalidation=93.0):
    from types import SimpleNamespace

    return [
        SimpleNamespace(level_type="resistance", level_value=resistance, scenario=scenario),
        SimpleNamespace(level_type="support", level_value=support, scenario=scenario),
        SimpleNamespace(level_type="invalidation", level_value=invalidation, scenario=scenario),
    ]


def test_evaluate_targets_bullish_success():
    window = pd.DataFrame({"high": [102, 106], "low": [99, 100], "close": [101, 105]})
    outcome = evaluate_targets(window, _mk_targets("bullish"), "bullish")
    assert outcome.scenario_realized == "bullish"
    assert outcome.outcome_score == pytest.approx(1.0)
    assert outcome.invalidation_touched_first is False


def test_evaluate_targets_invalidation_touched_first():
    window = pd.DataFrame({"high": [101, 102], "low": [90, 99], "close": [95, 100]})
    outcome = evaluate_targets(window, _mk_targets("bullish"), "bullish")
    assert outcome.invalidation_touched_first is True
    assert outcome.outcome_score == pytest.approx(0.1)


def test_evaluate_targets_neutral_stays_in_range():
    window = pd.DataFrame({"high": [101, 102], "low": [98, 97], "close": [100, 99]})
    outcome = evaluate_targets(window, _mk_targets("neutral"), "neutral")
    assert outcome.scenario_realized == "neutral"
    assert outcome.outcome_score == pytest.approx(0.7)


def test_evaluate_targets_bearish_success():
    window = pd.DataFrame({"high": [101, 102], "low": [94, 90], "close": [96, 92]})
    outcome = evaluate_targets(window, _mk_targets("bearish"), "bearish")
    assert outcome.scenario_realized == "bearish"
    assert outcome.outcome_score == pytest.approx(1.0)


def test_evaluate_targets_mixed_both_touched():
    window = pd.DataFrame({"high": [106, 101], "low": [99, 94], "close": [104, 96]})
    outcome = evaluate_targets(window, _mk_targets("neutral"), "neutral")
    assert outcome.scenario_realized == "mixed"


def test_evaluate_targets_bullish_target_hit_then_invalidation_later():
    # Resistance is hit first (bar 0), invalidation only breaks afterward (bar 1) —
    # scenario_realized still flips to "bearish" since invalidation did eventually hit,
    # but invalidation_touched_first is False -> hits the 0.2 fallback score.
    window = pd.DataFrame({"high": [106, 101], "low": [99, 90], "close": [105, 92]})
    outcome = evaluate_targets(window, _mk_targets("bullish"), "bullish")
    assert outcome.scenario_realized == "bearish"
    assert outcome.invalidation_touched_first is False
    assert outcome.outcome_score == pytest.approx(0.2)


def test_evaluate_targets_bearish_invalidation_is_resistance_touch():
    # For a bearish scenario, invalidation_touched_first compares against support_touch_pos.
    window = pd.DataFrame({"high": [106, 101], "low": [99, 99], "close": [104, 100]})
    outcome = evaluate_targets(window, _mk_targets("bearish"), "bearish")
    assert outcome.scenario_realized == "bullish"


def test_evaluate_targets_missing_levels_are_ignored():
    from types import SimpleNamespace

    targets = [SimpleNamespace(level_type="resistance", level_value=105.0, scenario="bullish")]
    window = pd.DataFrame({"high": [106.0], "low": [99.0], "close": [105.0]})
    outcome = evaluate_targets(window, targets, "bullish")
    assert outcome.levels_touched["support"] is False
    assert outcome.levels_touched["invalidation"] is False
    assert outcome.scenario_realized == "bullish"


# --- run_self_audit_once end-to-end ---

def test_run_self_audit_creates_result_for_bullish_success(monkeypatch, db_session):
    analysis = _make_completed_analysis(db_session, requested_at=datetime(2025, 12, 8, tzinfo=timezone.utc))
    daily = _synthetic_daily({"2025-12-09": (106.0, 107.0, 105.0)})  # next bar after analysis: resistance broken
    monkeypatch.setattr("app.audit.self_audit.get_market_data_provider", lambda: FakeMarketDataAdapter(daily))

    # as_of is far enough past requested_at that all three horizons (5, 10, 20) are
    # simultaneously eligible in this one run, not just horizon 5.
    summary = run_self_audit_once(db_session, as_of=date(2026, 1, 20))
    assert summary[5] == 1
    assert summary[10] == 1
    assert summary[20] == 1

    results = db_session.query(AuditResult).filter(AuditResult.analysis_id == analysis.id).all()
    assert len(results) == 3
    horizon_5_result = next(r for r in results if r.horizon_days == 5)
    assert horizon_5_result.scenario_realized == "bullish"
    assert float(horizon_5_result.outcome_score) == pytest.approx(1.0)

    stats = db_session.get(AccuracyStats, ("trending_up", 5))
    assert stats is not None
    assert stats.total_audited == 1
    assert float(stats.avg_outcome) == pytest.approx(1.0)


def test_run_self_audit_is_idempotent_per_horizon(monkeypatch, db_session):
    _make_completed_analysis(db_session, requested_at=datetime(2025, 12, 8, tzinfo=timezone.utc))
    daily = _synthetic_daily({"2025-12-09": (106.0, 107.0, 105.0)})
    monkeypatch.setattr("app.audit.self_audit.get_market_data_provider", lambda: FakeMarketDataAdapter(daily))

    first = run_self_audit_once(db_session, as_of=date(2026, 1, 20))
    second = run_self_audit_once(db_session, as_of=date(2026, 1, 20))
    assert first[5] == 1
    assert second[5] == 0  # already audited at horizon 5, not re-audited


def test_run_self_audit_skips_analysis_with_no_targets(monkeypatch, db_session):
    record = repository.create_analysis(
        db_session, symbol="NOTGT", market_open=True, data_provider="fake", deterministic_json={},
        scores={"technical": 50, "volatility": 50, "liquidity": 50, "risk": 50, "overall_confidence": 50},
        regime="ranging", status="data_only",
    )
    record.requested_at = datetime(2025, 12, 8, tzinfo=timezone.utc)
    db_session.commit()
    repository.update_analysis_report(db_session, record.id, report_text_ar="x", llm_provider="fake", llm_input_tokens=1, llm_output_tokens=1, total_cost_usd=0.01, status="completed")
    # deliberately no audit_targets created for this analysis

    daily = _synthetic_daily({})
    monkeypatch.setattr("app.audit.self_audit.get_market_data_provider", lambda: FakeMarketDataAdapter(daily))

    summary = run_self_audit_once(db_session, as_of=date(2026, 1, 20))
    assert summary[5] == 0
    assert db_session.query(AuditResult).filter(AuditResult.analysis_id == record.id).count() == 0


def test_run_self_audit_skips_gracefully_on_data_fetch_failure(monkeypatch, db_session):
    _make_completed_analysis(db_session, requested_at=datetime(2025, 12, 8, tzinfo=timezone.utc))

    class FailingAdapter(FakeMarketDataAdapter):
        def get_daily_ohlcv(self, symbol, lookback_days):
            raise RuntimeError("provider unavailable")

    monkeypatch.setattr("app.audit.self_audit.get_market_data_provider", lambda: FailingAdapter(pd.DataFrame()))

    summary = run_self_audit_once(db_session, as_of=date(2026, 1, 20))
    assert summary[5] == 0


def test_run_self_audit_skips_when_not_enough_bars_yet(monkeypatch, db_session):
    # analysis requested very recently relative to `as_of`/available bars -> horizon not reached
    analysis = _make_completed_analysis(db_session, requested_at=datetime(2026, 1, 19, tzinfo=timezone.utc))
    daily = _synthetic_daily({}, start="2025-12-01", periods=35)  # data ends right around the analysis date
    monkeypatch.setattr("app.audit.self_audit.get_market_data_provider", lambda: FakeMarketDataAdapter(daily))

    summary = run_self_audit_once(db_session, as_of=date(2026, 1, 20))
    assert summary[5] == 0
    assert db_session.query(AuditResult).filter(AuditResult.analysis_id == analysis.id).count() == 0


# --- evaluate_snipe_targets rubric unit tests (Snipe scanner, new feature) ---

def _mk_snipe_targets(zone1=105.0, zone2=110.0, invalidation=93.0):
    from types import SimpleNamespace

    targets = []
    if zone1 is not None:
        targets.append(SimpleNamespace(level_type="target_zone_1", level_value=zone1))
    if zone2 is not None:
        targets.append(SimpleNamespace(level_type="target_zone_2", level_value=zone2))
    targets.append(SimpleNamespace(level_type="invalidation", level_value=invalidation))
    return targets


def test_evaluate_snipe_targets_zone1_touched_first():
    window = pd.DataFrame({"high": [106.0, 111.0], "low": [99.0, 100.0]})
    outcome = evaluate_snipe_targets(window, _mk_snipe_targets())
    assert outcome.zone1_touched_first is True
    assert outcome.zone2_touched_first is True
    assert outcome.outcome_score == pytest.approx(1.0)
    assert outcome.scenario_realized == "bullish"


def test_evaluate_snipe_targets_invalidation_touched_first():
    window = pd.DataFrame({"high": [101.0, 111.0], "low": [90.0, 100.0]})
    outcome = evaluate_snipe_targets(window, _mk_snipe_targets())
    assert outcome.zone1_touched_first is False
    assert outcome.zone2_touched_first is False
    assert outcome.outcome_score == pytest.approx(0.0)
    assert outcome.scenario_realized == "bearish"


def test_evaluate_snipe_targets_neither_touched():
    window = pd.DataFrame({"high": [101.0, 102.0], "low": [99.0, 98.0]})
    outcome = evaluate_snipe_targets(window, _mk_snipe_targets())
    assert outcome.scenario_realized == "neutral"
    assert outcome.outcome_score == pytest.approx(0.0)


def test_evaluate_snipe_targets_missing_zone2_is_excluded_from_score():
    window = pd.DataFrame({"high": [106.0], "low": [99.0]})
    outcome = evaluate_snipe_targets(window, _mk_snipe_targets(zone2=None))
    assert outcome.zone1_touched_first is True
    assert outcome.zone2_touched is False
    assert outcome.outcome_score == pytest.approx(1.0)  # only zone1 counted, not averaged down by a missing zone2


# --- run_self_audit_once: kind-aware horizons (Snipe = 1 & 5 sessions, single = 5/10/20) ---

def _make_completed_snipe_analysis(
    db, symbol="SNIPE1", requested_at=None, zone1=105.0, zone2=110.0, invalidation=93.0,
):
    requested_at = requested_at or datetime(2026, 1, 5, tzinfo=timezone.utc)
    record = repository.create_analysis(
        db, symbol=symbol, market_open=False, data_provider="fake", deterministic_json={},
        scores={"technical": 50, "volatility": 50, "liquidity": 50, "risk": 50, "overall_confidence": 50},
        regime="trending_up", status="completed", kind="snipe",
    )
    record.requested_at = requested_at
    db.commit()
    repository.create_snipe_audit_targets(
        db, record.id, symbol, price_at_analysis=100.0,
        zone1_price=zone1, zone2_price=zone2, invalidation_price=invalidation,
    )
    db.refresh(record)
    return record


def test_run_self_audit_checks_snipe_at_1_and_5_sessions_only(monkeypatch, db_session):
    analysis = _make_completed_snipe_analysis(db_session, requested_at=datetime(2025, 12, 8, tzinfo=timezone.utc))
    daily = _synthetic_daily({"2025-12-09": (106.0, 107.0, 105.0)})  # zone1 (105) broken next bar
    monkeypatch.setattr("app.audit.self_audit.get_market_data_provider", lambda: FakeMarketDataAdapter(daily))

    summary = run_self_audit_once(db_session, as_of=date(2026, 1, 20))
    assert summary[1] == 1
    assert summary[5] == 1

    results = db_session.query(AuditResult).filter(AuditResult.analysis_id == analysis.id).all()
    assert {r.horizon_days for r in results} == {1, 5}  # never 10 or 20 for a snipe card
    horizon_5 = next(r for r in results if r.horizon_days == 5)
    assert horizon_5.levels_touched["zone1_touched_first"] is True
    assert horizon_5.scenario_realized == "bullish"

    # snipe cards never write into accuracy_stats (regime+horizon key doesn't fit)
    assert db_session.get(AccuracyStats, ("trending_up", 5)) is None


def test_run_self_audit_single_kind_unaffected_by_snipe_horizons(monkeypatch, db_session):
    _make_completed_analysis(db_session, requested_at=datetime(2025, 12, 8, tzinfo=timezone.utc))
    daily = _synthetic_daily({"2025-12-09": (106.0, 107.0, 105.0)})
    monkeypatch.setattr("app.audit.self_audit.get_market_data_provider", lambda: FakeMarketDataAdapter(daily))

    summary = run_self_audit_once(db_session, as_of=date(2026, 1, 20))
    assert summary[5] == 1
    assert summary[10] == 1
    assert summary[20] == 1
    assert summary[1] == 0  # no snipe-kind analyses exist in this test
