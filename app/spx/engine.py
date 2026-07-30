from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from math import isfinite

import pandas as pd

from app.config import Settings
from app.options.market_clock import MarketSession
from app.spx.schemas import (
    Direction,
    RankedSPXContract,
    SPXContract,
    SPXQuote,
    StrikeMode,
)


def _score(value: float) -> int:
    return max(0, min(100, round(value)))


def _age(stamp: datetime | None, now: datetime) -> int:
    if stamp is None:
        return 2_147_483_647
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return max(0, int((now - stamp.astimezone(timezone.utc)).total_seconds()))


def technical_analysis(frame: pd.DataFrame, quote: SPXQuote) -> dict:
    """Deterministic SPX levels and scores from provider values only."""
    if frame.empty or len(frame) < 30:
        raise ValueError("insufficient SPX history")
    data = frame.copy().sort_index()
    close = data["close"].astype(float)
    high = data["high"].astype(float)
    low = data["low"].astype(float)
    volume = data.get("volume", pd.Series(0.0, index=data.index)).astype(float)
    ema9 = close.ewm(span=9, adjust=False).mean()
    ema20 = close.ewm(span=20, adjust=False).mean()
    ema50 = close.ewm(span=50, adjust=False).mean()
    ema200 = close.ewm(span=200, adjust=False).mean()
    delta = close.diff()
    gains = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    losses = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    rsi = 100 - 100 / (1 + gains / losses.replace(0, float("nan")))
    macd_line = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
    macd_signal = macd_line.ewm(span=9, adjust=False).mean()
    previous = close.shift()
    tr = pd.concat([(high - low).abs(), (high - previous).abs(), (low - previous).abs()], axis=1).max(axis=1)
    atr = float(tr.rolling(14, min_periods=5).mean().iloc[-1])
    price = float(quote.price)
    trend_points = [
        price > float(ema9.iloc[-1]),
        float(ema9.iloc[-1]) > float(ema20.iloc[-1]),
        float(ema20.iloc[-1]) > float(ema50.iloc[-1]),
        float(macd_line.iloc[-1]) > float(macd_signal.iloc[-1]),
    ]
    bullish = sum(trend_points)
    bearish = sum(not item for item in trend_points)
    if bullish >= 3:
        direction = Direction.CALL
    elif bearish >= 3:
        direction = Direction.PUT
    else:
        direction = Direction.NONE
    momentum = abs(float(close.pct_change(5).iloc[-1] or 0)) * 100
    clarity = _score(max(bullish, bearish) / 4 * 100)
    momentum_score = _score(35 + min(65, momentum * 650))
    support = float(low.tail(30).min())
    resistance = float(high.tail(30).max())
    opening = data.tail(min(15, len(data)))
    typical = (high + low + close) / 3
    vwap = float((typical * volume).sum() / volume.sum()) if volume.sum() else None
    target_move = max(atr, price * 0.002)
    entry = resistance + atr * 0.05 if direction == Direction.CALL else support - atr * 0.05
    invalidation = support if direction == Direction.CALL else resistance
    targets = (
        [entry + target_move * factor for factor in (0.75, 1.25, 1.8)]
        if direction == Direction.CALL
        else [entry - target_move * factor for factor in (0.75, 1.25, 1.8)]
    )
    risk = abs(entry - invalidation)
    reward = abs(targets[1] - entry)
    return {
        "price": round(price, 2),
        "bid": quote.bid,
        "ask": quote.ask,
        "spread": round((quote.ask - quote.bid), 4) if quote.bid is not None and quote.ask is not None else None,
        "vwap": round(vwap, 2) if vwap is not None else None,
        "ema9": round(float(ema9.iloc[-1]), 2),
        "ema20": round(float(ema20.iloc[-1]), 2),
        "ema50": round(float(ema50.iloc[-1]), 2),
        "ema200": round(float(ema200.iloc[-1]), 2),
        "rsi": round(float(rsi.iloc[-1]), 2) if pd.notna(rsi.iloc[-1]) else None,
        "macd": round(float(macd_line.iloc[-1]), 4),
        "macd_signal": round(float(macd_signal.iloc[-1]), 4),
        "atr": round(atr, 2),
        "opening_range_high": round(float(opening["high"].max()), 2),
        "opening_range_low": round(float(opening["low"].min()), 2),
        "previous_day_high": round(float(high.iloc[-2]), 2),
        "previous_day_low": round(float(low.iloc[-2]), 2),
        "overnight_high": None,
        "overnight_low": None,
        "premarket_high": None,
        "premarket_low": None,
        "support": round(support, 2),
        "resistance": round(resistance, 2),
        "liquidity_zones": [round(support, 2), round(resistance, 2)],
        "gap_pct": round((float(data["open"].iloc[-1]) / float(close.iloc[-2]) - 1) * 100, 2),
        "trend_1m": direction.value,
        "trend_5m": direction.value,
        "trend_15m": direction.value,
        "trend_1h": direction.value,
        "daily_trend": "call" if price > float(ema200.iloc[-1]) else "put",
        "market_regime": "trend" if clarity >= 75 else "mixed",
        "volatility_regime": "high" if atr / price > 0.006 else "normal",
        "expected_move": round(target_move, 2),
        "direction": direction.value,
        "direction_clarity_score": clarity,
        "momentum_score": momentum_score,
        "timeframe_alignment_score": clarity,
        "entry_condition": (
            f"اختراق {entry:.2f} والثبات فوقه مع تأكيد الزخم"
            if direction == Direction.CALL
            else f"كسر {entry:.2f} والثبات تحته مع تأكيد الزخم"
            if direction == Direction.PUT
            else "انتظار توافق الاتجاه والزخم"
        ),
        "entry": round(entry, 2) if direction != Direction.NONE else None,
        "invalidation": round(invalidation, 2) if direction != Direction.NONE else None,
        "stop": round(invalidation, 2) if direction != Direction.NONE else None,
        "targets": [round(item, 2) for item in targets] if direction != Direction.NONE else [],
        "risk_reward": round(reward / risk, 2) if risk and direction != Direction.NONE else 0,
        "valid_minutes": 5,
    }


def directional_scenario(technical: dict, news: list[dict]) -> tuple[Direction, dict | None, str]:
    direction = Direction(technical.get("direction", "none"))
    strong = [item for item in news if int(item.get("spx_impact_score", 0)) >= 80]
    opposing = any(
        (direction == Direction.CALL and item.get("potential_direction_ar") == "داعم للهبوط")
        or (direction == Direction.PUT and item.get("potential_direction_ar") == "داعم للصعود")
        for item in strong
    )
    if opposing:
        return Direction.NONE, None, "الخبر أقوى من التحليل"
    if direction == Direction.NONE or technical["direction_clarity_score"] < 70:
        return Direction.NONE, None, "السوق متضارب"
    scenario = {
        "direction": direction.value,
        "trigger": technical["entry_condition"],
        "entry": technical["entry"],
        "invalidation": technical["invalidation"],
        "stop": technical["stop"],
        "targets": technical["targets"],
        "risk_reward": technical["risk_reward"],
        "clarity_score": technical["direction_clarity_score"],
        "risk_score": _score(100 - technical["direction_clarity_score"] * 0.6),
        "valid_minutes": technical["valid_minutes"],
        "cancellation_reason_ar": "فشل الاختراق أو الكسر، خبر رسمي معاكس، أو تقادم البيانات.",
    }
    return direction, scenario, "قنص مشروط"


def _premium_scenarios(contract: SPXContract, mid: float, targets: list[float], underlying: float) -> list[dict]:
    rows = []
    labels = ("متحفظ", "أساسي", "متفائل")
    for label, target, days, iv_shift in zip(labels, targets, (0.2, 0.5, 1.0), (-0.02, 0.0, 0.03)):
        move = target - underlying
        delta_value = float(contract.delta or 0)
        gamma_value = float(contract.gamma or 0)
        theta_value = float(contract.theta or 0)
        vega_value = float(contract.vega or 0)
        estimate = mid + delta_value * move + 0.5 * gamma_value * move * move + theta_value * days + vega_value * iv_shift
        estimate = max(0.01, estimate)
        profit = (estimate - mid) * 100
        rows.append({
            "label": label,
            "spx_target": round(target, 2),
            "estimated_premium": round(estimate, 2),
            "profit_usd": round(profit, 2),
            "profit_pct": round((estimate / mid - 1) * 100, 2),
            "assumptions_ar": f"Delta/Gamma الحالية، {days:g} يوم، تغير IV بمقدار {iv_shift:+.2f}.",
        })
    return rows


def rank_contracts(
    contracts: list[SPXContract],
    *,
    direction: Direction,
    scenario: dict,
    underlying: float,
    mode: StrikeMode,
    settings: Settings,
    session: MarketSession,
    now: datetime | None = None,
) -> tuple[list[RankedSPXContract], dict[str, int]]:
    now = now or datetime.now(timezone.utc)
    rejected: Counter[str] = Counter()
    ranked: list[RankedSPXContract] = []
    delta_min, delta_max = (
        (settings.spx_near_strike_delta_min, settings.spx_near_strike_delta_max)
        if mode == StrikeMode.NEAR
        else (settings.spx_far_strike_delta_min, settings.spx_far_strike_delta_max)
    )
    for item in contracts:
        dte = (item.expiration.date() - now.date()).days
        if dte < 0:
            rejected["expired"] += 1; continue
        if dte == 0 and not settings.spx_allow_0dte:
            rejected["0dte_disabled"] += 1; continue
        if dte == 1 and not settings.spx_allow_1dte:
            rejected["1dte_disabled"] += 1; continue
        if dte > 21:
            rejected["dte"] += 1; continue
        if item.option_type != direction.value:
            rejected["direction_mismatch"] += 1; continue
        values = (item.bid, item.ask, item.delta, item.gamma, item.theta, item.vega, item.iv)
        if any(value is None or not isfinite(float(value)) for value in values):
            rejected["missing_quote_or_greeks"] += 1; continue
        if item.ask < item.bid or item.bid < 0:
            rejected["invalid_quote"] += 1; continue
        age = _age(item.quote_timestamp, now)
        if age > settings.spx_max_data_age_seconds:
            rejected["stale_quote"] += 1; continue
        volume, oi = int(item.volume or 0), int(item.open_interest or 0)
        if volume < settings.spx_min_volume or oi < settings.spx_min_open_interest:
            rejected["low_liquidity"] += 1; continue
        absolute_delta = abs(float(item.delta))
        if not delta_min <= absolute_delta <= delta_max:
            rejected["delta"] += 1; continue
        mid = (float(item.bid) + float(item.ask)) / 2
        if mid <= 0:
            rejected["invalid_quote"] += 1; continue
        spread = float(item.ask) - float(item.bid)
        spread_pct = spread / mid * 100
        if spread_pct > settings.spx_max_spread_pct:
            rejected["wide_spread"] += 1; continue
        distance = (float(item.strike) / underlying - 1) * 100
        if (direction == Direction.CALL and distance > 2.0) or (direction == Direction.PUT and distance < -2.0):
            rejected["deep_otm"] += 1; continue
        moneyness = "ATM" if abs(distance) <= 0.25 else (
            "ITM" if (direction == Direction.CALL and item.strike < underlying) or (direction == Direction.PUT and item.strike > underlying) else "OTM"
        )
        spread_score = _score(100 - spread_pct * 8)
        delta_center = (delta_min + delta_max) / 2
        delta_score = _score(100 - abs(absolute_delta - delta_center) * 300)
        strike_score = _score(100 - abs(distance) * (25 if mode == StrikeMode.NEAR else 15))
        liquidity = _score(spread_score * .5 + min(100, 35 + volume / 20) * .2 + min(100, 35 + oi / 100) * .3)
        theta_score = _score(100 - abs(float(item.theta)) / mid * 300)
        risk = _score(100 - liquidity * .45 - delta_score * .25 + (18 if dte <= 2 else 0) + (10 if mode == StrikeMode.FAR else 0))
        if risk > settings.spx_max_risk_score:
            rejected["risk"] += 1; continue
        suitability = _score(delta_score * .25 + strike_score * .25 + spread_score * .2 + liquidity * .2 + theta_score * .1)
        target_rows = _premium_scenarios(item, mid, scenario["targets"], underlying)
        premium_stop_conservative = max(0.01, mid * (1 - min(.45, .25 + float(item.iv) * .15)))
        premium_stop_cautious = max(0.01, mid * (1 - min(.30, .16 + float(item.iv) * .08)))
        break_even = item.strike + mid if direction == Direction.CALL else item.strike - mid
        ranked.append(RankedSPXContract(
            symbol=item.symbol,
            option_type=item.option_type,
            strike=item.strike,
            expiration=item.expiration.date().isoformat(),
            dte=dte,
            moneyness=moneyness,
            bid=round(float(item.bid), 2),
            ask=round(float(item.ask), 2),
            mid=round(mid, 2),
            last=round(float(item.last), 2) if item.last is not None else None,
            spread=round(spread, 2),
            spread_pct=round(spread_pct, 2),
            volume=volume,
            open_interest=oi,
            delta=round(float(item.delta), 4),
            gamma=round(float(item.gamma), 4),
            theta=round(float(item.theta), 4),
            vega=round(float(item.vega), 4),
            iv=round(float(item.iv), 4),
            break_even=round(break_even, 2),
            contract_cost=round(mid * 100, 2),
            distance_to_strike_pct=round(distance, 3),
            entry=round(mid, 2),
            premium_stop_conservative=round(premium_stop_conservative, 2),
            premium_stop_cautious=round(premium_stop_cautious, 2),
            target_scenarios=target_rows,
            suitability_score=suitability,
            liquidity_score=liquidity,
            risk_score=risk,
            ranking_components={
                "direction": 100, "strike": strike_score, "delta": delta_score,
                "spread": spread_score, "liquidity": liquidity, "theta": theta_score,
            },
            required_spx_move=round(abs(item.strike - underlying), 2),
            time_sensitivity="مرتفعة" if dte <= 2 or mode == StrikeMode.FAR else "متوسطة",
            quote_age_seconds=age,
            actionable=session.options_actionable,
        ))
    ranked.sort(key=lambda row: (row.suitability_score, row.liquidity_score, -row.risk_score), reverse=True)
    limit = 1 if any(item.dte == 0 for item in ranked) else 3
    return ranked[:limit], dict(rejected)


def escape_reason(
    *,
    technical: dict | None,
    session: MarketSession,
    data_age: int | None,
    news: list[dict],
    best: RankedSPXContract | None,
    settings: Settings,
) -> str | None:
    if data_age is None or data_age > settings.spx_max_data_age_seconds:
        return "البيانات قديمة"
    if not session.options_actionable:
        return "سوق الخيارات مغلق"
    if technical and technical.get("direction") == "none":
        return "تضارب الفريمات والزخم"
    if any(int(item.get("spx_impact_score", 0)) >= 90 for item in news):
        return "خبر رسمي عالي التأثير يحتاج إعادة تحليل"
    if best and best.risk_score > settings.spx_max_risk_score:
        return "مخاطرة العقد أعلى من الحد"
    return None
