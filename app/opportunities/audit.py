from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class AuditOutcome:
    entered_at: datetime | None
    target_1_hit: bool
    target_2_hit: bool
    stop_hit: bool
    outcome: str
    highest: float | None
    lowest: float | None


def evaluate_timeline(
    ticks: list[tuple[datetime, float]],
    entry_from: float,
    entry_to: float,
    stop: float,
    target_1: float,
    target_2: float,
) -> AuditOutcome:
    ordered = sorted(ticks, key=lambda item: item[0])
    entered_at = None
    after_entry: list[float] = []
    hit_1 = hit_2 = stopped = False
    for timestamp, price in ordered:
        if entered_at is None:
            if entry_from <= price <= entry_to:
                entered_at = timestamp
                after_entry.append(price)
            continue
        after_entry.append(price)
        if price <= stop:
            stopped = True
            break
        if price >= target_1:
            hit_1 = True
        if price >= target_2:
            hit_2 = True
    if entered_at is None:
        outcome = "entry_not_triggered"
    elif stopped:
        outcome = "stopped"
    elif hit_2:
        outcome = "target_2"
    elif hit_1:
        outcome = "target_1"
    else:
        outcome = "open_or_expired"
    return AuditOutcome(
        entered_at, hit_1, hit_2, stopped, outcome,
        max(after_entry) if after_entry else None,
        min(after_entry) if after_entry else None,
    )
