"""Cost Gate tests — SRS 23 requires exhaustive coverage of this module (100%)."""
from __future__ import annotations

import pytest

from app.core.cost_gate import CostGate
from app.db.models import CostLimits


def test_allows_call_under_cap(db_session, test_settings):
    gate = CostGate(db_session, test_settings)
    decision = gate.check_and_reserve(category="llm", provider="anthropic", estimated_cost=0.02)
    assert decision.allowed is True
    assert decision.ledger_id is not None
    assert decision.reason is None


def test_allows_call_exactly_at_cap_boundary(db_session, test_settings):
    gate = CostGate(db_session, test_settings)
    decision = gate.check_and_reserve(category="llm", provider="anthropic", estimated_cost=1.00)
    assert decision.allowed is True  # spent(0) + estimate(1.00) == cap(1.00) -> not over


def test_rejects_when_estimate_would_exceed_daily_cap(db_session, test_settings):
    gate = CostGate(db_session, test_settings)
    decision = gate.check_and_reserve(category="llm", provider="anthropic", estimated_cost=1.01)
    assert decision.allowed is False
    assert "اليومي" in decision.reason
    assert decision.ledger_id is None


def test_rejects_when_accumulated_daily_spend_exceeds_cap(db_session, test_settings):
    gate = CostGate(db_session, test_settings)
    first = gate.check_and_reserve(category="llm", provider="anthropic", estimated_cost=0.60)
    assert first.allowed is True
    second = gate.check_and_reserve(category="llm", provider="anthropic", estimated_cost=0.60)
    assert second.allowed is False
    assert "اليومي" in second.reason


def test_rejects_when_monthly_cap_exceeded_but_daily_ok(db_session, test_settings):
    # daily cap is large enough, but monthly cap (5.00) is exceeded across many small calls.
    # Raise the anomaly threshold so this test isolates cap logic from anomaly detection.
    test_settings.default_daily_cap_usd = 100.00
    test_settings.cost_anomaly_calls_per_minute = 100
    gate = CostGate(db_session, test_settings)
    for _ in range(5):
        decision = gate.check_and_reserve(category="market_data", provider="alpaca", estimated_cost=1.00)
        assert decision.allowed is True
    decision = gate.check_and_reserve(category="market_data", provider="alpaca", estimated_cost=0.01)
    assert decision.allowed is False
    assert "الشهري" in decision.reason


def test_manual_kill_switch_blocks_regardless_of_budget(db_session, test_settings):
    gate = CostGate(db_session, test_settings)
    gate.enable_kill_switch()
    decision = gate.check_and_reserve(category="llm", provider="anthropic", estimated_cost=0.001)
    assert decision.allowed is False
    assert "Kill-Switch" in decision.reason


def test_disable_kill_switch_restores_normal_operation(db_session, test_settings):
    gate = CostGate(db_session, test_settings)
    gate.enable_kill_switch()
    gate.disable_kill_switch()
    decision = gate.check_and_reserve(category="llm", provider="anthropic", estimated_cost=0.02)
    assert decision.allowed is True


def test_anomaly_pattern_auto_enables_kill_switch(db_session, test_settings):
    # test_settings.cost_anomaly_calls_per_minute == 3
    gate = CostGate(db_session, test_settings)
    for _ in range(3):
        decision = gate.check_and_reserve(category="llm", provider="anthropic", estimated_cost=0.001)
        assert decision.allowed is True
    fourth = gate.check_and_reserve(category="llm", provider="anthropic", estimated_cost=0.001)
    assert fourth.allowed is False
    assert "شاذ" in fourth.reason
    assert gate.get_limits().kill_switch_on is True

    # kill-switch now persists: even a tiny, well-budgeted request is blocked next call.
    fifth = gate.check_and_reserve(category="llm", provider="anthropic", estimated_cost=0.001)
    assert fifth.allowed is False
    assert "Kill-Switch" in fifth.reason


def test_record_actual_updates_ledger(db_session, test_settings):
    gate = CostGate(db_session, test_settings)
    decision = gate.check_and_reserve(category="llm", provider="anthropic", estimated_cost=0.02)
    entry = gate.record_actual(decision.ledger_id, 0.017)
    assert float(entry.actual_cost) == 0.017


def test_update_caps_changes_effective_limits(db_session, test_settings):
    gate = CostGate(db_session, test_settings)
    gate.update_caps(daily_cap_usd=0.05, monthly_cap_usd=None)
    decision = gate.check_and_reserve(category="llm", provider="anthropic", estimated_cost=0.10)
    assert decision.allowed is False
    assert "اليومي" in decision.reason
    assert gate.get_limits().monthly_cap_usd == 5.00  # untouched


def test_spend_summary_today_and_month(db_session, test_settings):
    gate = CostGate(db_session, test_settings)
    gate.check_and_reserve(category="llm", provider="anthropic", estimated_cost=0.25)
    today = gate.spend_summary("today")
    month = gate.spend_summary("month")
    assert today == {"spent": 0.25, "cap": 1.00, "pct": 25.0}
    assert month == {"spent": 0.25, "cap": 5.00, "pct": 5.0}


def test_spend_summary_invalid_period_raises(db_session, test_settings):
    gate = CostGate(db_session, test_settings)
    try:
        gate.spend_summary("year")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_missing_cost_limits_row_raises_runtime_error(db_session, test_settings):
    gate = CostGate(db_session, test_settings)
    db_session.query(CostLimits).delete()
    db_session.commit()
    with pytest.raises(RuntimeError):
        gate.check_and_reserve(category="llm", provider="anthropic", estimated_cost=0.01)


def test_actual_cost_preferred_over_estimated_when_summing(db_session, test_settings):
    gate = CostGate(db_session, test_settings)
    decision = gate.check_and_reserve(category="llm", provider="anthropic", estimated_cost=0.50)
    gate.record_actual(decision.ledger_id, 0.10)  # actual came in lower than estimate
    today = gate.spend_summary("today")
    assert today["spent"] == 0.10
