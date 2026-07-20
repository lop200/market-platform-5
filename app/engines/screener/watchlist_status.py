"""Graduated read-only status indicator for the options watchlist ("مراقب حالة قرائي",
owner request 2026-07-19 — supersedes an earlier hard "-5% sell alert" design the same
day). Pure Python, no LLM (CLAUDE.md rule 1), explicit formula (CLAUDE.md rule 2). This
module NEVER recommends a trade — it only classifies a contract's current reading into
one of five tiers and writes a plain-language explanation of why.

    decline_pct = max(0, (reference_price - current_price) / reference_price * 100)
    T = alert_threshold_pct (owner-adjustable, default 5%) — the "red" trigger point.

    tier 0 🟢 مرتاح   : decline_pct < 0.35×T
    tier 1 🟡 طبيعي   : 0.35×T <= decline_pct < 0.7×T
    tier 2 🟠 خلك حذر : 0.7×T <= decline_pct < T
    tier 3 🔴 تنبّه   : decline_pct >= T, OR the contract's translated invalidation price
                        has been breached
    tier 4 ⚫ خطر     : decline_pct >= 2×T, OR tier 3 AND (the contract is 0DTE with <= 1
                        hour left, OR its own daily Theta decay is >= 8% of the contract
                        price — either way, time decay is compounding an already-broken
                        reading)

Bands scale with the user's own adjustable threshold rather than a fixed percentage, so
"خلك حذر" always sits strictly between "طبيعي" and the user's chosen alert point no
matter what they set T to.

Speed component (owner request point 1: "% change + speed of movement + remaining
Theta"): if the decline is happening fast enough that it would cross the full threshold
within one hour at the current rate, the tier is bumped up by one (capped at 4) — a slow
3% drift over a day reads very differently from the same 3% in ten minutes.

    speed_pct_per_hour = decline_pct / hours_since_added  # only evaluated once hours_since_added >= 0.25
    escalate one tier if speed_pct_per_hour >= T
"""
from __future__ import annotations

from dataclasses import dataclass

ORANGE_BAND_FRACTION = 0.7  # decline_pct >= 0.7*T -> orange
YELLOW_BAND_FRACTION = 0.35  # decline_pct >= 0.35*T -> yellow
BLACK_DECLINE_MULTIPLE = 2.0  # decline_pct >= 2*T -> black outright
BLACK_HOURS_TO_EXPIRY = 1.0  # tier 3 + 0DTE + <= this many hours left -> black
SEVERE_DAILY_THETA_DECAY_PCT = 8.0  # tier 3 + theta eating >= this % of the contract/day -> black
SPEED_ESCALATION_MULTIPLE = 1.0  # speed_pct_per_hour >= T -> escalate one tier
# Below this many hours since adding, there isn't enough of a time base to judge "speed"
# at all (a 0.3% wobble in the first 90 seconds would otherwise read as an extreme rate)
# -> speed escalation is skipped entirely until this much time has actually passed.
SPEED_MIN_OBSERVATION_HOURS = 0.25

TIER_INFO = {
    0: {"code": "green", "emoji": "🟢", "mood": "😎", "label": "مرتاح — القراءة سليمة"},
    1: {"code": "yellow", "emoji": "🟡", "mood": "🙂", "label": "طبيعي — حركة عادية"},
    2: {"code": "orange", "emoji": "🟠", "mood": "😐", "label": "خلك حذر — السعر يقترب من منطقة ضعف"},
    3: {"code": "red", "emoji": "🔴", "mood": "😰", "label": "تنبّه — القراءة الفنية انكسرت"},
    4: {"code": "black", "emoji": "⚫", "mood": "💀", "label": "المنطقة الخطرة — Theta يأكل العقد والقراءة سلبية"},
}


@dataclass(frozen=True)
class WatchlistStatus:
    tier: int  # 0-4
    code: str  # green/yellow/orange/red/black
    emoji: str
    mood_emoji: str
    label: str
    message: str  # colloquial one-line explanation ("نزل 12% من مرجعك + باقي 3 ساعات على الانتهاء")
    change_pct: float  # signed, current vs reference
    decline_pct: float  # max(0, -change_pct)
    invalidation_broken: bool


def _format_hours(hours: float) -> str:
    whole_hours = int(hours)
    minutes = round((hours - whole_hours) * 60)
    if minutes == 60:
        whole_hours += 1
        minutes = 0
    if whole_hours <= 0:
        return f"{minutes} دقيقة"
    if minutes == 0:
        return f"{whole_hours} ساعة"
    return f"{whole_hours} ساعة و{minutes} دقيقة"


def compute_watchlist_status(
    *,
    reference_price: float,
    current_price: float,
    alert_threshold_pct: float,
    invalidation_price: float | None,
    hours_since_added: float,
    is_0dte: bool,
    hours_to_expiry: float | None,
    daily_theta_decay_pct: float | None = None,
) -> WatchlistStatus:
    """Classify one watched contract's current reading. See module docstring for the
    full declared formula. `hours_to_expiry` is only meaningful (and only used) when
    `is_0dte` is True. `daily_theta_decay_pct` is optional (not available at add-time,
    before a first live refresh) — when present it can independently push a tier-3
    reading to tier-4 even for a contract that isn't literally 0DTE."""
    threshold = max(alert_threshold_pct, 0.1)  # guard against a 0/negative user input
    change_pct = (current_price - reference_price) / reference_price * 100 if reference_price else 0.0
    decline_pct = max(0.0, -change_pct)

    invalidation_broken = invalidation_price is not None and current_price <= invalidation_price

    if decline_pct >= BLACK_DECLINE_MULTIPLE * threshold:
        tier = 4
    elif decline_pct >= threshold or invalidation_broken:
        tier = 3
    elif decline_pct >= ORANGE_BAND_FRACTION * threshold:
        tier = 2
    elif decline_pct >= YELLOW_BAND_FRACTION * threshold:
        tier = 1
    else:
        tier = 0

    # theta is negative for a long call (py_vollib convention, engines/options/greeks.py) —
    # compare magnitude, since "severe decay" is about how much value is bleeding per day.
    severe_theta = daily_theta_decay_pct is not None and abs(daily_theta_decay_pct) >= SEVERE_DAILY_THETA_DECAY_PCT
    near_expiry = is_0dte and hours_to_expiry is not None and hours_to_expiry <= BLACK_HOURS_TO_EXPIRY
    if tier == 3 and (near_expiry or severe_theta):
        tier = 4

    fast_move = False
    if hours_since_added >= SPEED_MIN_OBSERVATION_HOURS:
        speed_pct_per_hour = decline_pct / hours_since_added
        fast_move = speed_pct_per_hour >= SPEED_ESCALATION_MULTIPLE * threshold
        if fast_move and tier < 4:
            tier += 1

    info = TIER_INFO[tier]

    parts: list[str] = []
    if decline_pct > 0.01:
        parts.append(f"نزل {decline_pct:.0f}% من مرجعك")
    elif change_pct > 0.01:
        parts.append(f"طالع {change_pct:.0f}% فوق مرجعك")
    else:
        parts.append("قريب جداً من مرجعك")
    if fast_move:
        parts.append("والحركة سريعة")
    if invalidation_broken:
        parts.append("وكسر مستوى الإبطال الفني")
    if is_0dte and hours_to_expiry is not None:
        parts.append(f"وباقي {_format_hours(hours_to_expiry)} على الانتهاء")
    if severe_theta:
        parts.append(f"وTheta يأكل {abs(daily_theta_decay_pct):.0f}% من سعر العقد يومياً")
    message = " + ".join(parts)

    return WatchlistStatus(
        tier=tier,
        code=info["code"],
        emoji=info["emoji"],
        mood_emoji=info["mood"],
        label=info["label"],
        message=message,
        change_pct=round(change_pct, 2),
        decline_pct=round(decline_pct, 2),
        invalidation_broken=invalidation_broken,
    )
