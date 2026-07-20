"""Cost Gate — the ONLY path to a paid external call (SRS 16, CLAUDE.md rule 3).

Architectural rule (SRS 16.3, "درس Replit"): no code path reaches a market-data or LLM
call without going through `check_and_reserve()` first. Adapters are never called
directly by API routes; only the orchestrator calls them, and only after this gate
approves.

Flow per SRS 4.1 step [4] / 16.1:
1. Manual kill-switch on?              -> reject
2. Anomaly (>N calls/minute)?          -> auto-enable kill-switch, reject
3. daily_spent + estimate > daily_cap? -> reject
4. monthly_spent + estimate > cap?     -> reject
5. otherwise: record the ESTIMATED cost in cost_ledger and allow.

After the real call completes, the caller MUST report the actual cost via
`record_actual()` so the ledger reflects reality (SRS 7.2).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import repository
from app.db.models import CostLedger, CostLimits


@dataclass(frozen=True)
class CostGateDecision:
    allowed: bool
    reason: str | None
    ledger_id: int | None
    daily_spent_usd: float
    monthly_spent_usd: float
    daily_cap_usd: float
    monthly_cap_usd: float


class CostGate:
    def __init__(self, db: Session, settings: Settings | None = None):
        self.db = db
        self.settings = settings or get_settings()
        repository.get_or_create_cost_limits(
            db, self.settings.default_daily_cap_usd, self.settings.default_monthly_cap_usd
        )

    @staticmethod
    def _day_start(now: datetime) -> datetime:
        return now.replace(hour=0, minute=0, second=0, microsecond=0)

    @staticmethod
    def _month_start(now: datetime) -> datetime:
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    def _current_limits(self) -> CostLimits:
        limits = self.db.get(CostLimits, 1)
        if limits is None:
            raise RuntimeError("cost_limits row missing — CostGate.__init__ should have created it")
        return limits

    def check_and_reserve(
        self,
        *,
        category: str,
        provider: str,
        estimated_cost: float,
        analysis_id=None,
        tokens_in: int | None = None,
        tokens_out: int | None = None,
    ) -> CostGateDecision:
        now = datetime.now(timezone.utc)
        limits = self._current_limits()
        daily_cap = float(limits.daily_cap_usd)
        monthly_cap = float(limits.monthly_cap_usd)
        daily_spent = repository.sum_cost_since(self.db, self._day_start(now))
        monthly_spent = repository.sum_cost_since(self.db, self._month_start(now))

        def reject(reason: str) -> CostGateDecision:
            return CostGateDecision(
                allowed=False,
                reason=reason,
                ledger_id=None,
                daily_spent_usd=daily_spent,
                monthly_spent_usd=monthly_spent,
                daily_cap_usd=daily_cap,
                monthly_cap_usd=monthly_cap,
            )

        if limits.kill_switch_on:
            return reject("Kill-Switch مفعّل — تم إيقاف كل الاستدعاءات المدفوعة")

        one_minute_ago = now - timedelta(seconds=60)
        recent_calls = repository.count_calls_since(self.db, one_minute_ago)
        if recent_calls >= self.settings.cost_anomaly_calls_per_minute:
            repository.set_kill_switch(self.db, True)
            return reject(
                "تم رصد نمط استدعاءات شاذ (احتمال خلل بالكود) — تفعيل Kill-Switch تلقائياً"
            )

        if daily_spent + estimated_cost > daily_cap:
            return reject(f"بلغت السقف اليومي ({daily_cap:.2f}$) — الطلب مرفوض")

        if monthly_spent + estimated_cost > monthly_cap:
            return reject(f"بلغت السقف الشهري ({monthly_cap:.2f}$) — الطلب مرفوض")

        ledger_entry = repository.record_estimated_cost(
            self.db,
            category=category,
            provider=provider,
            estimated_cost=estimated_cost,
            analysis_id=analysis_id,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )
        return CostGateDecision(
            allowed=True,
            reason=None,
            ledger_id=ledger_entry.id,
            daily_spent_usd=daily_spent,
            monthly_spent_usd=monthly_spent,
            daily_cap_usd=daily_cap,
            monthly_cap_usd=monthly_cap,
        )

    def record_actual(self, ledger_id: int, actual_cost: float) -> CostLedger:
        return repository.record_actual_cost(self.db, ledger_id, actual_cost)

    def enable_kill_switch(self) -> CostLimits:
        return repository.set_kill_switch(self.db, True)

    def disable_kill_switch(self) -> CostLimits:
        return repository.set_kill_switch(self.db, False)

    def update_caps(self, daily_cap_usd: float | None, monthly_cap_usd: float | None) -> CostLimits:
        return repository.update_cost_caps(self.db, daily_cap_usd, monthly_cap_usd)

    def get_limits(self) -> CostLimits:
        return self._current_limits()

    def spend_summary(self, period: str) -> dict:
        """period: 'today' or 'month' — used by GET /cost/today and /cost/month (SRS 8)."""
        now = datetime.now(timezone.utc)
        limits = self._current_limits()
        if period == "today":
            since = self._day_start(now)
            cap = float(limits.daily_cap_usd)
        elif period == "month":
            since = self._month_start(now)
            cap = float(limits.monthly_cap_usd)
        else:
            raise ValueError(f"unknown period '{period}', expected 'today' or 'month'")
        spent = repository.sum_cost_since(self.db, since)
        pct = round((spent / cap) * 100, 1) if cap > 0 else 0.0
        return {"spent": round(spent, 5), "cap": cap, "pct": pct}
