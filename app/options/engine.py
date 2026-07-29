from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timezone

from app.config import Settings
from app.options.market_clock import market_session
from app.options.schemas import (
    OptionChainResult,
    OptionType,
    RankedOptionContract,
    RawOptionContract,
)


def _clamp(value: float) -> int:
    return int(round(max(0, min(100, value))))


def _age_seconds(value: datetime | None, now: datetime) -> int:
    if value is None:
        return 10**9
    aware = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return max(0, int((now - aware.astimezone(timezone.utc)).total_seconds()))


def rank_option_chain(
    stock_analysis: dict,
    contracts: list[RawOptionContract],
    settings: Settings,
    *,
    now: datetime | None = None,
) -> OptionChainResult:
    generated = now or datetime.now(timezone.utc)
    if generated.tzinfo is None:
        generated = generated.replace(tzinfo=timezone.utc)
    session = market_session(generated)
    symbol = str(stock_analysis.get("symbol") or "").upper()
    stock_valid = (
        stock_analysis.get("status") == "conditional_entry"
        and bool(stock_analysis.get("data_quality", {}).get("valid_for_plan"))
        and bool(stock_analysis.get("trade_plan"))
    )
    base = dict(
        symbol=symbol,
        stock_status=str(stock_analysis.get("status") or "no_trade"),
        stock_first_gate_passed=stock_valid,
        options_enabled=settings.options_enabled,
        options_session_open=session.options_actionable,
        feed=settings.alpaca_options_feed if settings.options_enabled else None,
        generated_at=generated,
    )
    if not settings.options_enabled:
        return OptionChainResult(status="disabled", **base, warnings_ar=["الخيارات معطلة من الإعدادات"])
    if not stock_valid:
        return OptionChainResult(
            status="no_trade",
            **base,
            warnings_ar=["لم تُجلب سلسلة الخيارات لأن فرصة السهم الأساسية غير صالحة"],
        )

    underlying = float(stock_analysis["quote"]["price"])
    direction = str(stock_analysis.get("trend") or "")
    targets = stock_analysis["trade_plan"].get("targets") or []
    stock_target_1 = float(targets[0]["price"]) if targets else underlying * 1.02
    today = generated.date()
    rejected: Counter[str] = Counter()
    ranked: list[RankedOptionContract] = []
    for item in contracts:
        dte = (item.expiration - today).days
        if dte in {0, 1} or dte < settings.options_min_dte or dte > settings.options_max_dte:
            rejected["dte"] += 1
            continue
        if item.bid is None or item.ask is None or item.bid <= 0 or item.ask <= item.bid:
            rejected["invalid_quote"] += 1
            continue
        mid = (item.bid + item.ask) / 2
        spread_pct = (item.ask - item.bid) / mid * 100
        if spread_pct > settings.options_max_spread_pct:
            rejected["wide_spread"] += 1
            continue
        volume, oi = int(item.volume or 0), int(item.open_interest or 0)
        if volume < settings.options_min_volume or oi < settings.options_min_open_interest:
            rejected["low_liquidity"] += 1
            continue
        greek_values = (item.delta, item.gamma, item.theta, item.vega, item.iv)
        if any(value is None for value in greek_values):
            rejected["missing_greeks"] += 1
            continue
        age = _age_seconds(item.quote_timestamp, generated)
        if age > settings.options_max_quote_age_seconds:
            rejected["stale_quote"] += 1
            continue
        delta = float(item.delta)
        target_delta = 0.55 if item.option_type == OptionType.CALL else -0.55
        delta_fit = max(0, 100 - abs(delta - target_delta) * 180)
        liquidity = _clamp(35 + min(volume, 500) / 10 + min(oi, 3000) / 60 - spread_pct * 2)
        moneyness = abs(item.strike - underlying) / underlying * 100
        suitability = _clamp(delta_fit * .5 + liquidity * .35 + max(0, 100 - moneyness * 10) * .15)
        iv = float(item.iv)
        risk = _clamp(35 + spread_pct * 2 + max(0, iv - .35) * 55 + moneyness * 3)
        directional_bonus = (
            8 if ("صاعد" in direction and item.option_type == OptionType.CALL)
            or ("هابط" in direction and item.option_type == OptionType.PUT) else 0
        )
        score = round(suitability * .55 + liquidity * .3 + (100 - risk) * .15 + directional_bonus, 2)
        expected_move = abs(stock_target_1 - underlying)
        option_move_1 = max(.01, abs(delta) * expected_move + .5 * float(item.gamma) * expected_move**2)
        target_1 = round(mid + option_move_1, 2)
        target_2 = round(mid + option_move_1 * 1.6, 2)
        stop = round(max(.01, mid * (1 - min(.45, .18 + risk / 500))), 2)
        probability = round(max(.2, min(.8, abs(delta) * .7 + suitability / 500)), 2)
        warnings = []
        if iv >= .65:
            warnings.append("تقلب ضمني مرتفع: خطر IV Crush")
        if not session.options_actionable:
            warnings.append("سوق الخيارات مغلق؛ العقد للمتابعة فقط")
        ranked.append(RankedOptionContract(
            symbol=item.symbol,
            underlying_symbol=symbol,
            option_type=item.option_type,
            strike=item.strike,
            expiration=item.expiration,
            dte=dte,
            bid=round(item.bid, 2),
            ask=round(item.ask, 2),
            mid=round(mid, 2),
            spread_pct=round(spread_pct, 2),
            volume=volume,
            open_interest=oi,
            delta=round(delta, 4),
            gamma=round(float(item.gamma), 4),
            theta=round(float(item.theta), 4),
            vega=round(float(item.vega), 4),
            iv=round(iv, 4),
            break_even=round(item.strike + mid if item.option_type == OptionType.CALL else item.strike - mid, 2),
            contract_cost=round(item.ask * 100, 2),
            suitability_score=suitability,
            liquidity_score=liquidity,
            risk_score=risk,
            ranking_score=score,
            target_1=target_1,
            target_2=target_2,
            stop_loss=stop,
            scenario_probability=probability,
            quote_timestamp=item.quote_timestamp or generated,
            quote_age_seconds=age,
            feed=item.feed,
            actionable=session.options_actionable,
            warnings_ar=warnings,
        ))
    ranked.sort(key=lambda value: value.ranking_score, reverse=True)
    shortlisted = ranked[: max(1, min(3, settings.options_contract_limit))]
    best_call = next((item for item in ranked if item.option_type == OptionType.CALL), None)
    best_put = next((item for item in ranked if item.option_type == OptionType.PUT), None)
    return OptionChainResult(
        status="ready" if shortlisted else "no_contract",
        **base,
        contracts_considered=len(contracts),
        contracts_rejected=sum(rejected.values()),
        best_call=best_call,
        best_put=best_put,
        ranked_contracts=shortlisted,
        rejection_reasons=dict(rejected),
        warnings_ar=(
            ["Paper Trading فقط — لا يوجد تنفيذ حقيقي تلقائي"]
            + ([] if session.options_actionable else ["العقود غير قابلة للتنفيذ خارج الجلسة الرسمية"])
        ),
    )

