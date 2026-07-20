"""Support/resistance detection via local-extrema clustering (SRS 10.2).

Two stages: (1) find local swing highs/lows over a rolling window, (2) cluster swing
points within 0.5x ATR of each other, weighting each resulting level by touch count,
recency, and volume. The strength-score weights below are a declared, calibratable
formula (SRS 10.2 requires a "declared strength score", not a specific weighting) —
tune the constants once real outcome data exists (SRS 15.4), the shape stays the same.
"""
from __future__ import annotations

import pandas as pd

from app.engines.deterministic.schemas import LevelStrength, Levels

STRENGTH_WEIGHT_TOUCHES = 0.4
STRENGTH_WEIGHT_RECENCY = 0.3
STRENGTH_WEIGHT_VOLUME = 0.3
TOUCHES_SATURATION = 5  # touch count at/above which the touches component maxes out
VOLUME_SATURATION_MULTIPLE = 1.5  # avg-volume-at-touch multiple of overall avg volume that maxes out
INVALIDATION_ATR_BUFFER = 0.25  # buffer below the strongest support, in ATR multiples (SRS 19 wording)


def find_swing_points(high: pd.Series, low: pd.Series, window: int = 5) -> pd.DataFrame:
    """A swing high/low at bar i is the max/min within the centered [i-window, i+window]."""
    span = 2 * window + 1
    is_swing_high = high == high.rolling(window=span, center=True).max()
    is_swing_low = low == low.rolling(window=span, center=True).min()
    return pd.DataFrame({"is_swing_high": is_swing_high.fillna(False), "is_swing_low": is_swing_low.fillna(False)})


def _cluster_points(points: list[dict], threshold: float) -> list[list[dict]]:
    if not points:
        return []
    points_sorted = sorted(points, key=lambda p: p["price"])
    clusters: list[list[dict]] = [[points_sorted[0]]]
    for point in points_sorted[1:]:
        if point["price"] - clusters[-1][-1]["price"] <= threshold:
            clusters[-1].append(point)
        else:
            clusters.append([point])
    return clusters


def _score_cluster(cluster: list[dict], last_bar_index: int, lookback_bars: int, overall_avg_volume: float) -> LevelStrength:
    prices = [p["price"] for p in cluster]
    volumes = [p["volume"] for p in cluster]
    bar_indices = [p["bar_index"] for p in cluster]

    touches = len(cluster)
    most_recent_bar = max(bar_indices)
    last_touch_bars_ago = last_bar_index - most_recent_bar
    avg_volume_at_touches = float(sum(volumes) / len(volumes))
    level_price = float(sum(prices) / len(prices))

    touches_component = min(touches / TOUCHES_SATURATION, 1.0)
    recency_component = max(0.0, 1 - last_touch_bars_ago / max(lookback_bars, 1))
    volume_component = min(
        avg_volume_at_touches / (overall_avg_volume * VOLUME_SATURATION_MULTIPLE), 1.0
    ) if overall_avg_volume > 0 else 0.0

    strength_score = (
        STRENGTH_WEIGHT_TOUCHES * touches_component
        + STRENGTH_WEIGHT_RECENCY * recency_component
        + STRENGTH_WEIGHT_VOLUME * volume_component
    )

    return LevelStrength(
        price=round(level_price, 4),
        touches=touches,
        last_touch_bars_ago=int(last_touch_bars_ago),
        avg_volume_at_touches=round(avg_volume_at_touches, 2),
        strength_score=round(strength_score, 4),
    )


def detect_levels(
    daily: pd.DataFrame,
    atr_value: float,
    current_price: float,
    window: int = 5,
    max_levels: int = 3,
) -> Levels:
    swings = find_swing_points(daily["high"], daily["low"], window=window)
    points: list[dict] = []
    for bar_index, (idx, row) in enumerate(daily.iterrows()):
        if swings["is_swing_high"].iloc[bar_index]:
            points.append({"price": float(row["high"]), "bar_index": bar_index, "volume": float(row["volume"])})
        if swings["is_swing_low"].iloc[bar_index]:
            points.append({"price": float(row["low"]), "bar_index": bar_index, "volume": float(row["volume"])})

    threshold = max(atr_value * 0.5, 1e-9)
    clusters = _cluster_points(points, threshold)

    last_bar_index = len(daily) - 1
    lookback_bars = len(daily)
    overall_avg_volume = float(daily["volume"].mean())

    supports: list[LevelStrength] = []
    resistances: list[LevelStrength] = []
    for cluster in clusters:
        scored = _score_cluster(cluster, last_bar_index, lookback_bars, overall_avg_volume)
        if scored.price < current_price:
            supports.append(scored)
        elif scored.price > current_price:
            resistances.append(scored)
        # a cluster exactly at current_price is neither — negligible edge case

    supports.sort(key=lambda lv: lv.strength_score, reverse=True)
    resistances.sort(key=lambda lv: lv.strength_score, reverse=True)

    invalidation = supports[0].price - INVALIDATION_ATR_BUFFER * atr_value if supports else None

    return Levels(
        supports=supports[:max_levels],
        resistances=resistances[:max_levels],
        invalidation=round(invalidation, 4) if invalidation is not None else None,
    )
