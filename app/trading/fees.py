from __future__ import annotations


def _capped(value: float, minimum: float, maximum: float) -> float:
    return min(max(value, minimum), maximum)


def estimate_sahm_us_stock_round_trip_fees(
    entry_price: float,
    exit_price: float,
    quantity: int,
    *,
    vat_pct: float = 15.0,
    conservative_round_trip_floor_usd: float = 4.0,
) -> dict:
    """Deterministic schedule estimate with a conservative observed-cost floor."""
    if entry_price <= 0 or exit_price <= 0 or quantity <= 0:
        return {"total_usd": 0.0, "buy_usd": 0.0, "sell_usd": 0.0, "vat_usd": 0.0}

    def commission(price: float) -> float:
        value = price * quantity
        minimum = 0.49 if price <= 5 else 1.99
        return _capped(0.015 * quantity, minimum, value * 0.015)

    def settlement(price: float) -> float:
        return _capped(0.003 * quantity, 0.01, price * quantity * 0.07)

    def cat_fee(price: float) -> float:
        return _capped(0.000003 * quantity, 0.01, price * quantity)

    buy_before_vat = commission(entry_price) + settlement(entry_price) + cat_fee(entry_price)
    sec_fee = max(exit_price * quantity * 0.0000206, 0.01)
    activity_fee = min(max(0.000195 * quantity, 0.01), 9.79)
    sell_before_vat = (
        commission(exit_price) + settlement(exit_price) + cat_fee(exit_price)
        + sec_fee + activity_fee
    )
    vat = (buy_before_vat + sell_before_vat) * vat_pct / 100
    buy_total = round(buy_before_vat * (1 + vat_pct / 100), 2)
    sell_total = round(sell_before_vat * (1 + vat_pct / 100), 2)
    schedule_total = round(buy_total + sell_total, 2)
    conservative_total = max(schedule_total, round(conservative_round_trip_floor_usd, 2))
    return {
        "buy_usd": buy_total,
        "sell_usd": sell_total,
        "vat_usd": round(vat, 2),
        "schedule_total_usd": schedule_total,
        "total_usd": conservative_total,
        "conservative_floor_usd": round(conservative_round_trip_floor_usd, 2),
        "conservative_floor_applied": conservative_total > schedule_total,
        "schedule": "sahm_us_stock_reference",
        "estimated": True,
    }
