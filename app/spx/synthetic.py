from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, time, timezone
from math import exp, log1p
from statistics import median
from time import perf_counter

from app.config import Settings
from app.options.market_clock import MarketSession, NEW_YORK, is_early_close, nyse_holidays
from app.spx.schemas import SPXContract, SPXSyntheticValue, SyntheticPairResult

SECONDS_PER_YEAR = 365.0 * 24 * 60 * 60
SOURCE = "Alpaca OPRA Synthetic"


def _score(value: float) -> int:
    return max(0, min(100, round(value)))


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _age_seconds(stamp: datetime, now: datetime) -> int:
    return max(0, int((_aware(now).astimezone(timezone.utc) - _aware(stamp).astimezone(timezone.utc)).total_seconds()))


def _parse_setting_stamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return _aware(stamp)
    except ValueError:
        return None


def settlement_at(expiration: date, settlement_type: str) -> datetime:
    """Return the actual settlement instant for explicitly supported SPX series."""
    if expiration in nyse_holidays(expiration.year) or expiration.weekday() >= 5:
        raise ValueError("settlement_on_closed_day")
    if settlement_type == "PM_CASH":
        close = time(13) if is_early_close(expiration) else time(16)
        return datetime.combine(expiration, close, tzinfo=NEW_YORK)
    if settlement_type == "AM_CASH":
        return datetime.combine(expiration, time(9, 30), tzinfo=NEW_YORK)
    raise ValueError("unknown_settlement_type")


def weighted_median(values: list[float], weights: list[float]) -> float:
    if not values or len(values) != len(weights):
        raise ValueError("weighted_median requires aligned values")
    ordered = sorted(zip(values, weights), key=lambda item: item[0])
    total = sum(max(0.0, item[1]) for item in ordered)
    if total <= 0:
        return float(median(values))
    threshold = total / 2
    cumulative = 0.0
    for value, weight in ordered:
        cumulative += max(0.0, weight)
        if cumulative >= threshold:
            return float(value)
    return float(ordered[-1][0])


def _quantile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("empty distribution")
    position = (len(ordered) - 1) * q
    lower = int(position)
    upper = min(len(ordered) - 1, lower + 1)
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def _empty(
    now: datetime,
    status: str,
    message: str,
    reasons: Counter | dict | None = None,
    *,
    elapsed_ms: int = 0,
) -> SPXSyntheticValue:
    return SPXSyntheticValue(
        calculation_timestamp=now,
        provider_status=status,
        status_message_ar=message,
        rejection_reasons=dict(reasons or {}),
        calculation_time_ms=elapsed_ms,
    )


def calculate_synthetic_value(
    contracts: list[SPXContract],
    settings: Settings,
    session: MarketSession,
    *,
    now: datetime | None = None,
) -> SPXSyntheticValue:
    started = perf_counter()
    now = _aware(now or datetime.now(timezone.utc))
    rejected: Counter[str] = Counter()
    if not settings.options_enabled or not settings.spx_synthetic_enabled:
        return _empty(now, "unavailable", "مزود SPX الضمني غير مفعل.")
    if not settings.spx_synthetic_paper_only or not settings.spx_paper_only:
        return _empty(now, "unavailable", "المزود الضمني يعمل في Paper Trading فقط.")
    if not session.options_actionable:
        return _empty(
            now,
            "options_closed",
            "سوق الخيارات مغلق — آخر قراءة للمراقبة فقط",
        )
    if not contracts or not any(item.feed.lower() == "opra" for item in contracts):
        return _empty(now, "opra_unavailable", "بيانات OPRA غير متاحة")
    if settings.spx_risk_free_rate is None:
        return _empty(
            now,
            "unavailable",
            "معدل الفائدة الخالي من المخاطر غير مضبوط.",
            {"missing_risk_free_rate": 1},
        )

    grouped: dict[tuple[str, str, str], dict[float, dict[str, SPXContract]]] = defaultdict(
        lambda: defaultdict(dict)
    )
    for contract in contracts:
        if contract.option_type not in {"call", "put"}:
            continue
        expiry_key = contract.expiration.date().isoformat()
        settlement = contract.settlement_type or ""
        style = (contract.exercise_style or "").lower()
        grouped[(expiry_key, settlement, style)][contract.strike][contract.option_type] = contract
    candidates = [
        (key, strikes)
        for key, strikes in grouped.items()
        if sum(1 for sides in strikes.values() if {"call", "put"} <= set(sides))
    ]
    if not candidates:
        return _empty(now, "insufficient_pairs", "عدد أزواج Call وPut غير كافٍ")
    candidates.sort(
        key=lambda item: (
            -sum(1 for sides in item[1].values() if {"call", "put"} <= set(sides)),
            item[0][0],
        )
    )
    (expiration_text, settlement_type, exercise_style), strike_groups = candidates[0]
    pairs_requested = sum(
        1 for sides in strike_groups.values() if {"call", "put"} <= set(sides)
    )
    if settlement_type not in {"PM_CASH", "AM_CASH"}:
        return _empty(
            now, "invalid_quotes", "نوع التسوية غير معروف",
            {"unknown_settlement_type": pairs_requested},
        )
    if exercise_style != "european":
        return _empty(
            now, "invalid_quotes", "نوع ممارسة العقد غير صالح لحساب Put-Call Parity",
            {"non_european": pairs_requested},
        )
    try:
        settlement = settlement_at(date.fromisoformat(expiration_text), settlement_type)
    except (TypeError, ValueError):
        return _empty(
            now, "unavailable", "تعذر حساب وقت التسوية الفعلي",
            {"invalid_settlement_time": pairs_requested},
        )
    seconds_to_settlement = (settlement.astimezone(timezone.utc) - now.astimezone(timezone.utc)).total_seconds()
    if seconds_to_settlement <= 0:
        return _empty(now, "unavailable", "لا يوجد Expiration صالح", {"expired": pairs_requested})
    years_to_settlement = seconds_to_settlement / SECONDS_PER_YEAR
    rate = float(settings.spx_risk_free_rate)
    factor = exp(rate * years_to_settlement)

    rate_stamp = _parse_setting_stamp(settings.spx_risk_free_rate_updated_at)
    dividend_stamp = _parse_setting_stamp(settings.spx_dividend_yield_updated_at)
    rate_age = _age_seconds(rate_stamp, now) if rate_stamp else None
    dividend_age = _age_seconds(dividend_stamp, now) if dividend_stamp else None
    max_rate_age = settings.spx_rate_max_age_days * 86400
    rate_fresh = rate_age is not None and rate_age <= max_rate_age
    dividend_fresh = dividend_age is not None and dividend_age <= max_rate_age
    spot_allowed = (
        settings.spx_allow_spot_estimate
        and settings.spx_dividend_yield is not None
        and rate_fresh
        and dividend_fresh
    )

    valid_pairs: list[SyntheticPairResult] = []
    ages: list[int] = []
    for strike, sides in strike_groups.items():
        call, put = sides.get("call"), sides.get("put")
        if call is None or put is None:
            rejected["missing_side"] += 1
            continue
        if (
            call.settlement_type != put.settlement_type
            or call.exercise_style != put.exercise_style
            or call.expiration != put.expiration
        ):
            rejected["contract_mismatch"] += 1
            continue
        quote_values = (call.bid, call.ask, put.bid, put.ask)
        if any(value is None for value in quote_values):
            rejected["missing_bid_ask"] += 1
            continue
        call_bid, call_ask, put_bid, put_ask = map(float, quote_values)
        if min(call_bid, call_ask, put_bid, put_ask) < 0 or call_ask < call_bid or put_ask < put_bid:
            rejected["invalid_bid_ask"] += 1
            continue
        if call.quote_timestamp is None or put.quote_timestamp is None:
            rejected["missing_quote_time"] += 1
            continue
        call_stamp, put_stamp = _aware(call.quote_timestamp), _aware(put.quote_timestamp)
        if call_stamp.astimezone(NEW_YORK).date() != put_stamp.astimezone(NEW_YORK).date():
            rejected["quote_date_mismatch"] += 1
            continue
        time_diff = abs((call_stamp - put_stamp).total_seconds())
        if time_diff > settings.spx_synthetic_max_pair_time_diff_seconds:
            rejected["pair_time_diff"] += 1
            continue
        call_age, put_age = _age_seconds(call_stamp, now), _age_seconds(put_stamp, now)
        quote_age = max(call_age, put_age)
        if quote_age > settings.spx_synthetic_max_quote_age_seconds:
            rejected["stale_quote"] += 1
            continue
        call_mid, put_mid = (call_bid + call_ask) / 2, (put_bid + put_ask) / 2
        if call_mid <= 0 or put_mid <= 0:
            rejected["invalid_mid"] += 1
            continue
        call_spread_pct = (call_ask - call_bid) / call_mid * 100
        put_spread_pct = (put_ask - put_bid) / put_mid * 100
        if max(call_spread_pct, put_spread_pct) > settings.spx_synthetic_max_spread_pct:
            rejected["wide_spread"] += 1
            continue
        call_oi, put_oi = int(call.open_interest or 0), int(put.open_interest or 0)
        minimum_oi = min(call_oi, put_oi)
        if minimum_oi < settings.spx_synthetic_min_open_interest:
            rejected["low_open_interest"] += 1
            continue
        forward = float(strike) + factor * (call_mid - put_mid)
        lower = float(strike) + factor * (call_bid - put_ask)
        upper = float(strike) + factor * (call_ask - put_bid)
        if lower > upper or forward <= 0:
            rejected["illogical_value"] += 1
            continue
        spot = (
            forward * exp(-(rate - float(settings.spx_dividend_yield)) * years_to_settlement)
            if spot_allowed else None
        )
        spread_score = _score(100 - max(call_spread_pct, put_spread_pct) * 5)
        freshness_score = _score(100 - quote_age / max(1, settings.spx_synthetic_max_quote_age_seconds) * 100)
        alignment_score = _score(
            100 - time_diff / max(0.001, settings.spx_synthetic_max_pair_time_diff_seconds) * 100
        )
        oi_score = _score(35 + log1p(minimum_oi) * 9)
        volume_score = _score(25 + log1p(min(int(call.volume or 0), int(put.volume or 0))) * 10)
        liquidity_score = _score(spread_score * .45 + oi_score * .30 + volume_score * .15 + alignment_score * .10)
        quality_score = _score(freshness_score * .35 + spread_score * .30 + alignment_score * .20 + liquidity_score * .15)
        weight = max(1.0, quality_score * (0.65 + liquidity_score / 300))
        ages.append(quote_age)
        valid_pairs.append(SyntheticPairResult(
            strike=float(strike),
            expiration=expiration_text,
            settlement_type=settlement_type,
            exercise_style=exercise_style,
            call_symbol=call.symbol,
            put_symbol=put.symbol,
            call_mid=round(call_mid, 4),
            put_mid=round(put_mid, 4),
            call_spread_pct=round(call_spread_pct, 3),
            put_spread_pct=round(put_spread_pct, 3),
            pair_forward_value=round(forward, 4),
            pair_spot_estimate=round(spot, 4) if spot is not None else None,
            lower_bound=round(lower, 4),
            upper_bound=round(upper, 4),
            quote_age_seconds=quote_age,
            quote_time_difference_seconds=round(time_diff, 3),
            open_interest_score=oi_score,
            liquidity_score=liquidity_score,
            pair_quality_score=quality_score,
            weight=round(weight, 4),
        ))

    if len(valid_pairs) < settings.spx_synthetic_min_pairs:
        status = "stale" if rejected.get("stale_quote") else "insufficient_pairs"
        message = "بيانات العقود قديمة" if status == "stale" else "عدد أزواج Call وPut غير كافٍ"
        result = _empty(now, status, message, rejected)
        result.pairs_requested = pairs_requested
        result.rejected_pairs = pairs_requested - len(valid_pairs)
        result.expiration_used = expiration_text
        result.settlement_type = settlement_type
        return result

    initial_pairs = len(valid_pairs)
    initial_median = float(median([item.pair_forward_value for item in valid_pairs]))
    deviations = [abs(item.pair_forward_value - initial_median) for item in valid_pairs]
    mad = float(median(deviations))
    outlier_limit = max(settings.spx_synthetic_max_dispersion_points * 2, mad * 3)
    cleaned = [
        item for item in valid_pairs
        if abs(item.pair_forward_value - initial_median) <= outlier_limit
    ]
    outliers_removed = len(valid_pairs) - len(cleaned)
    if len(cleaned) < settings.spx_synthetic_min_pairs:
        result = _empty(now, "wide_dispersion", "القيمة الضمنية غير مستقرة", {"outliers": outliers_removed})
        result.pairs_requested = pairs_requested
        result.rejected_pairs = pairs_requested - len(cleaned)
        result.outliers_removed = outliers_removed
        return result

    estimate = initial_median
    convergence = [round(estimate, 4)]
    selected = cleaned
    iterations = 0
    for _ in range(2):
        iterations += 1
        selected = sorted(
            cleaned,
            key=lambda item: (
                abs(item.strike - estimate),
                -item.pair_quality_score,
                item.quote_age_seconds,
            ),
        )[: settings.spx_synthetic_max_pairs]
        weights = [
            item.weight / (1 + abs(item.strike - estimate) / max(1.0, estimate * .002))
            for item in selected
        ]
        new_estimate = weighted_median(
            [item.pair_forward_value for item in selected], weights
        )
        convergence.append(round(new_estimate, 4))
        change = abs(new_estimate - estimate)
        estimate = new_estimate
        if change <= settings.spx_synthetic_max_convergence_points:
            break
    converged = (
        len(convergence) >= 2
        and abs(convergence[-1] - convergence[-2])
        <= settings.spx_synthetic_max_convergence_points
    )
    weights = [
        item.weight / (1 + abs(item.strike - estimate) / max(1.0, estimate * .002))
        for item in selected
    ]
    forward = weighted_median([item.pair_forward_value for item in selected], weights)
    lower_bound = weighted_median([item.lower_bound for item in selected], weights)
    upper_bound = weighted_median([item.upper_bound for item in selected], weights)
    range_width = max(0.0, upper_bound - lower_bound)
    forward_values = [item.pair_forward_value for item in selected]
    dispersion = _quantile(forward_values, .75) - _quantile(forward_values, .25)
    liquidity_score = _score(float(median([item.liquidity_score for item in selected])))
    dispersion_score = _score(
        100 - dispersion / max(.001, settings.spx_synthetic_max_dispersion_points) * 100
    )
    range_score = _score(
        100 - range_width / max(.001, settings.spx_synthetic_max_range_width_points) * 100
    )
    freshness_score = _score(
        100
        - float(median([item.quote_age_seconds for item in selected]))
        / max(1, settings.spx_synthetic_max_quote_age_seconds)
        * 100
    )
    count_score = _score(
        len(selected) / max(settings.spx_synthetic_min_pairs, settings.spx_synthetic_max_pairs) * 100
    )
    data_quality = _score(
        freshness_score * .25
        + float(median([item.pair_quality_score for item in selected])) * .25
        + dispersion_score * .20
        + range_score * .20
        + count_score * .10
    )
    confidence = _score(data_quality * .55 + liquidity_score * .30 + count_score * .15 - 8)
    if not rate_fresh:
        confidence = _score(confidence - 8)
    if not spot_allowed:
        confidence = _score(confidence - 3)
    spot_estimate = (
        weighted_median(
            [float(item.pair_spot_estimate) for item in selected if item.pair_spot_estimate is not None],
            [weight for item, weight in zip(selected, weights) if item.pair_spot_estimate is not None],
        )
        if spot_allowed else None
    )

    status = "ready"
    message = "قيمة SPX الضمنية جاهزة"
    if not converged:
        status, message = "wide_dispersion", "القيمة الضمنية لم تتقارب — لا صفقة"
        rejected["no_convergence"] += 1
    elif dispersion > settings.spx_synthetic_max_dispersion_points:
        status, message = "wide_dispersion", "القيمة الضمنية غير مستقرة"
    elif range_width > settings.spx_synthetic_max_range_width_points:
        status, message = "wide_dispersion", "نطاق التسعير واسع — لا صفقة"
    elif (
        confidence < settings.spx_synthetic_min_confidence_score
        or data_quality < settings.spx_synthetic_min_data_quality_score
    ):
        status, message = "low_confidence", "المصدر المؤقت منخفض الثقة — انتظر"
    if not (min(item.strike for item in selected) * .95 <= forward <= max(item.strike for item in selected) * 1.05):
        status, message = "unavailable", "القيمة الناتجة خارج نطاق منطقي"
        rejected["outside_strike_range"] += 1

    elapsed = round((perf_counter() - started) * 1000)
    return SPXSyntheticValue(
        synthetic_forward_value=round(forward, 4),
        synthetic_spot_estimate=round(spot_estimate, 4) if spot_estimate is not None else None,
        lower_bound=round(lower_bound, 4),
        upper_bound=round(upper_bound, 4),
        implied_range_width_points=round(range_width, 4),
        pairs_requested=pairs_requested,
        pairs_used=len(selected),
        rejected_pairs=pairs_requested - len(selected),
        outliers_removed=outliers_removed,
        initial_pairs=initial_pairs,
        refined_pairs=len(selected),
        iterations=iterations,
        expiration_used=expiration_text,
        settlement_type=settlement_type,
        calculation_timestamp=now,
        oldest_quote_age_seconds=max(item.quote_age_seconds for item in selected),
        newest_quote_age_seconds=min(item.quote_age_seconds for item in selected),
        median_quote_age_seconds=round(float(median([item.quote_age_seconds for item in selected])), 2),
        max_pair_time_diff_seconds=round(max(item.quote_time_difference_seconds for item in selected), 3),
        dispersion_points=round(dispersion, 4),
        convergence_points=convergence,
        confidence_score=confidence,
        data_quality_score=data_quality,
        liquidity_score=liquidity_score,
        dispersion_score=dispersion_score,
        range_quality_score=range_score,
        source=SOURCE,
        provider_status=status,
        status_message_ar=message,
        rejection_reasons=dict(rejected),
        pairs=selected,
        risk_free_rate_used=rate,
        dividend_yield_used=(
            float(settings.spx_dividend_yield)
            if settings.spx_dividend_yield is not None else None
        ),
        risk_free_rate_source=settings.spx_risk_free_rate_source,
        dividend_yield_source=settings.spx_dividend_yield_source,
        risk_free_rate_updated_at=settings.spx_risk_free_rate_updated_at,
        dividend_yield_updated_at=settings.spx_dividend_yield_updated_at,
        risk_free_rate_age_seconds=rate_age,
        dividend_yield_age_seconds=dividend_age,
        spot_estimate_label_ar=(
            "تقدير Spot منخفض الثقة" if spot_estimate is not None else "Spot estimate معطل أو غير موثوق"
        ),
        calculation_time_ms=elapsed,
    )
