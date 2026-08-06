from app.trading.fees import estimate_sahm_us_stock_round_trip_fees


def test_sahm_fee_estimate_includes_both_sides_regulatory_fees_and_vat():
    fees = estimate_sahm_us_stock_round_trip_fees(100, 105, 10)
    assert fees["buy_usd"] >= 1.99
    assert fees["sell_usd"] >= 1.99
    assert fees["vat_usd"] > 0
    assert fees["total_usd"] == round(fees["buy_usd"] + fees["sell_usd"], 2)
    assert fees["estimated"] is True


def test_low_price_commission_uses_low_price_schedule():
    low = estimate_sahm_us_stock_round_trip_fees(3, 3.25, 10)
    high = estimate_sahm_us_stock_round_trip_fees(30, 32.5, 10)
    assert low["total_usd"] >= 4.0
    assert low["conservative_floor_applied"] is True
    assert low["total_usd"] < high["total_usd"]
