"""Snipe scanner accuracy panel ("لوحة دقة الماسح التاريخية") — new feature, not in SRS.

Reads real self-audit outcomes (audit/self_audit.py::evaluate_snipe_targets, via
`AuditResult.levels_touched`) for the most recent audited Snipe cards, at the 5-session
horizon (the more decisive checkpoint vs the 1-session quick-read). Declared formula
(CLAUDE.md rule 2): hit rate = (# cards where the zone touched before invalidation) /
(# audited cards), over the last `limit` audited cards.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.db import repository
from app.engines.screener.snipe_schemas import SnipeAccuracyPanel

ACCURACY_WINDOW = 100
ACCURACY_HORIZON_DAYS = 5
MIN_SAMPLE_SIZE = 5  # below this, the UI shows "insufficient data" instead of a misleading %


def compute_snipe_accuracy_panel(db: Session) -> SnipeAccuracyPanel:
    results = repository.get_recent_snipe_audit_results(
        db, horizon_days=ACCURACY_HORIZON_DAYS, limit=ACCURACY_WINDOW
    )
    n = len(results)
    if n == 0:
        return SnipeAccuracyPanel(
            sample_size=0, zone1_hit_rate=None, zone2_hit_rate=None, horizon_days=ACCURACY_HORIZON_DAYS
        )

    zone1_hits = sum(1 for r in results if (r.levels_touched or {}).get("zone1_touched_first"))
    zone2_hits = sum(1 for r in results if (r.levels_touched or {}).get("zone2_touched_first"))
    return SnipeAccuracyPanel(
        sample_size=n,
        zone1_hit_rate=zone1_hits / n,
        zone2_hit_rate=zone2_hits / n,
        horizon_days=ACCURACY_HORIZON_DAYS,
    )
