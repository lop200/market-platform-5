from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timezone

from app.config import Settings
from app.options.market_clock import market_session, serialize_market_session
from app.options.schemas import (
    OptionChainResult,
    OptionTargetScenario,
    OptionType,
    RankedOptionContract,
    RawOptionContract,
)
from app.options.sniper import ShortDTEOptionSniper


def _clamp(value: float) -> int:
    return int(round(max(0, min(100, value))))


def _age_seconds(value: datetime | None, now: datetime) -> int:
    if value is None:
        return 10**9
    aware = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return max(0, int((now - aware.astimezone(timezone.utc)).total_seconds()))


def _direction(value: str) -> tuple[str | None, OptionType | None]:
    normalized = value.strip().lower()
    if any(token in normalized for token in ("صاعد", "bull", "uptrend")):
        return "bullish", OptionType.CALL
    if any(token in normalized for token in ("هابط", "bear", "downtrend")):
        return "bearish", OptionType.PUT
    return None, None


def _earnings_context(stock_analysis: dict, today: date, settings: Settings) -> tuple:
    event = stock_analysis.get("earnings") or {}
    raw_date = event.get("earnings_date") or event.get("date")
    try:
        earnings_date = date.fromisoformat(raw_date) if raw_date else None
    except (TypeError, ValueError):
        earnings_date = None
    risk_level = str(event.get("earnings_risk") or "")
    days = (earnings_date - today).days if earnings_date else None
    if days is None:
        return None, "unknown", False, 70
    if event.get("post_earnings_enabled") or days < 0:
        return earnings_date, "past", False, 70
    if event.get("prevent_new_entry") or risk_level == "very_high":
        return earnings_date, "very_high", True, 5
    if risk_level == "high" or days <= settings.options_earnings_risk_days:
        return earnings_date, "high", True, 25
    if days <= 14:
        return earnings_date, "medium", True, 55
    return earnings_date, "low", False, 90


def _option_earnings_metrics(
    contracts: list[RawOptionContract],
    underlying: float,
    earnings_date: date | None,
    generated: datetime,
) -> tuple[float | None, float | None]:
    """Estimate an ATM straddle move from fresh Bid/Ask mids, never Last."""
    if not contracts or underlying <= 0:
        return None, None
    expirations = sorted(
        {
            item.expiration for item in contracts
            if item.expiration >= (earnings_date or generated.date())
        }
    )
    if not expirations:
        return None, None
    expiration = expirations[0]
    strikes = sorted(
        {item.strike for item in contracts if item.expiration == expiration},
        key=lambda strike: abs(strike - underlying),
    )
    if not strikes:
        return None, None
    strike = strikes[0]
    pair = [
        item for item in contracts
        if item.expiration == expiration and item.strike == strike
    ]
    call = next((item for item in pair if item.option_type == OptionType.CALL), None)
    put = next((item for item in pair if item.option_type == OptionType.PUT), None)
    if (
        call is None or put is None
        or call.bid is None or call.ask is None
        or put.bid is None or put.ask is None
        or call.ask < call.bid or put.ask < put.bid
    ):
        return None, None
    call_mid = (call.bid + call.ask) / 2
    put_mid = (put.bid + put.ask) / 2
    implied = round((call_mid + put_mid) / underlying * 100, 2)
    iv_values = [float(item.iv) for item in (call, put) if item.iv is not None]
    average_iv = round(sum(iv_values) / len(iv_values), 4) if iv_values else None
    return implied, average_iv


def _moneyness(
    option_type: OptionType, strike: float, underlying: float
) -> tuple[str, float, float]:
    distance = (strike - underlying) / underlying * 100
    absolute_distance = abs(distance)
    if absolute_distance <= 1:
        label = "ATM"
    elif (option_type == OptionType.CALL and strike < underlying) or (
        option_type == OptionType.PUT and strike > underlying
    ):
        label = "ITM"
    else:
        label = "OTM"
    otm_distance = max(0.0, distance if option_type == OptionType.CALL else -distance)
    return label, round(distance, 2), otm_distance


def _scenario(
    *,
    name: str,
    label_ar: str,
    target: float,
    underlying: float,
    entry: float,
    delta: float,
    gamma: float,
    theta: float,
    vega: float,
    iv_change_pct: float,
    expected_days: float,
    spread: float,
) -> OptionTargetScenario:
    stock_move = target - underlying
    iv_decimal_move = iv_change_pct / 100
    theoretical_change = (
        delta * stock_move
        + 0.5 * gamma * stock_move**2
        + theta * expected_days
        + vega * iv_decimal_move
    )
    liquidity_discount = spread * (0.5 if theoretical_change >= 0 else 0.25)
    estimated = round(max(0.01, entry + theoretical_change - liquidity_discount), 2)
    profit = round((estimated - entry) * 100, 2)
    profit_pct = round((estimated / entry - 1) * 100, 2)
    return OptionTargetScenario(
        name=name,
        label_ar=label_ar,
        underlying_target=round(target, 2),
        estimated_contract_price=estimated,
        profit_usd=profit,
        profit_pct=profit_pct,
        expected_days=round(expected_days, 1),
        iv_change_pct=iv_change_pct,
        assumptions_ar=[
            f"حركة السهم من {underlying:.2f} إلى {target:.2f}",
            f"Delta {delta:.3f} وGamma {gamma:.3f}",
            f"Theta لمدة {expected_days:.1f} يوم",
            f"تغير IV مفترض {iv_change_pct:+.0f}%",
            "خصم نصف السبريد من السعر النظري عند الربح",
        ],
    )


def _result_base(
    stock_analysis: dict, settings: Settings, generated: datetime
) -> tuple[dict, OptionType | None]:
    session = market_session(generated)
    symbol = str(stock_analysis.get("symbol") or "").upper()
    direction, preferred_side = _direction(str(stock_analysis.get("trend") or ""))
    earnings_date, earnings_risk, iv_crush, _ = _earnings_context(
        stock_analysis, generated.date(), settings
    )
    quote = stock_analysis.get("quote") or {}
    quality = stock_analysis.get("data_quality", {})
    stock_quote_age = max(
        (
            int(value) for value in (
                quality.get("bid_age_seconds"),
                quality.get("ask_age_seconds"),
                quote.get("age_seconds"),
            )
            if value is not None
        ),
        default=None,
    )
    expiry_raw = (stock_analysis.get("trade_plan") or {}).get(
        "expires_at", stock_analysis.get("expires_at")
    )
    try:
        analysis_expiry = datetime.fromisoformat(str(expiry_raw).replace("Z", "+00:00"))
        if analysis_expiry.tzinfo is None:
            analysis_expiry = analysis_expiry.replace(tzinfo=timezone.utc)
        analysis_current = generated.astimezone(analysis_expiry.tzinfo) < analysis_expiry
    except (TypeError, ValueError):
        analysis_current = False
    stock_valid = (
        stock_analysis.get("status") == "conditional_entry"
        and bool(stock_analysis.get("data_quality", {}).get("valid_for_plan"))
        and bool(stock_analysis.get("trade_plan"))
        and quote.get("bid") is not None
        and quote.get("ask") is not None
        and float(quote.get("ask") or 0) >= float(quote.get("bid") or 0) > 0
        and stock_quote_age is not None
        and int(stock_quote_age) <= settings.max_quote_age_seconds
        and preferred_side is not None
        and analysis_current
    )
    scenario_type = (stock_analysis.get("strategy") or {}).get("name_ar")
    return (
        {
            "symbol": symbol,
            "stock_status": str(stock_analysis.get("status") or "no_trade"),
            "stock_first_gate_passed": stock_valid,
            "options_enabled": settings.options_enabled,
            "options_session_open": session.options_actionable,
            "feed": settings.alpaca_options_feed if settings.options_enabled else None,
            "generated_at": generated,
            "direction": direction,
            "scenario_type": scenario_type,
            "preferred_option_type": preferred_side,
            "direction_reason_ar": (
                "الاتجاه الصاعد في السهم الأساسي يسمح بدراسة عقود Call فقط."
                if preferred_side == OptionType.CALL
                else "الاتجاه الهابط في السهم الأساسي يسمح بدراسة عقود Put فقط."
                if preferred_side == OptionType.PUT
                else "الأطر الزمنية لا تعطي اتجاهًا واضحًا لاختيار نوع العقد."
            ),
            "earnings_date": earnings_date,
            "earnings_risk": earnings_risk,
            "iv_crush_warning": iv_crush,
            "market": serialize_market_session(session),
        },
        preferred_side,
    )


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
    base, preferred_side = _result_base(stock_analysis, settings, generated)
    session = market_session(generated)
    if not settings.options_enabled:
        base["market"]["options_status"] = "disabled"
        base["market"]["options_label_ar"] = "غير مفعل"
        return OptionChainResult(
            status="disabled",
            **base,
            warnings_ar=["قسم الخيارات غير مفعل لأن OPTIONS_ENABLED=false."],
        )
    if not settings.options_paper_only:
        base["market"]["options_status"] = "disabled"
        base["market"]["options_label_ar"] = "غير مفعل"
        return OptionChainResult(
            status="disabled",
            **base,
            warnings_ar=[
                "تحليل العقود يعمل في وضع Paper Trading فقط؛ التنفيذ Live محظور."
            ],
        )
    if not base["stock_first_gate_passed"]:
        return OptionChainResult(
            status="no_trade",
            **base,
            warnings_ar=[
                "لا توجد فرصة فنية مكتملة على السهم الأساسي، لذلك لم يتم اختيار عقد أوبشن حاليًا."
            ],
        )

    quote = stock_analysis["quote"]
    underlying = float(quote["price"])
    sniper = ShortDTEOptionSniper(settings)
    sniper_universe = sniper.select_universe(
        contracts, underlying_price=underlying, now=generated
    )
    candidate_contracts = (
        sniper_universe.contracts if sniper.enabled else contracts
    )
    plan = stock_analysis["trade_plan"]
    targets = [
        float(item["price"]) for item in (plan.get("targets") or [])
        if item.get("price") is not None
    ]
    if not targets:
        return OptionChainResult(
            status="no_trade",
            **base,
            warnings_ar=["لا توجد أهداف سهم حتمية يمكن اشتقاق أهداف العقد منها."],
        )
    stock_entry = float(plan["entry_from"])
    stock_stop = float(plan["stop"])
    valid_minutes = int(plan.get("valid_minutes") or stock_analysis.get("valid_for_minutes") or 0)
    expires_raw = plan.get("expires_at") or stock_analysis.get("expires_at")
    try:
        expires_at = datetime.fromisoformat(str(expires_raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        expires_at = generated

    earnings_date, earnings_risk, iv_crush, earnings_score = _earnings_context(
        stock_analysis, generated.date(), settings
    )
    implied_move_pct, earnings_iv = _option_earnings_metrics(
        contracts, underlying, earnings_date, generated
    )
    event_days = (
        (earnings_date - generated.date()).days if earnings_date is not None else None
    )
    capital_usd = (
        settings.options_account_size_usd
        if sniper.enabled
        else settings.default_capital_sar / settings.usd_sar_rate
    )
    news_context = stock_analysis.get("news_context") or {}
    news_penalty = 25 if news_context.get("raise_risk") else 0
    rejected: Counter[str] = Counter()
    ranked: list[RankedOptionContract] = []

    market_date = session.new_york_time.date()
    for item in candidate_contracts:
        dte = (item.expiration - market_date).days
        if item.expiration < market_date:
            rejected["expired_or_0_1dte"] += 1
            continue
        if not sniper.enabled and dte in {0, 1}:
            rejected["expired_or_0_1dte"] += 1
            continue
        minimum_dte = (
            settings.options_scalp_min_dte
            if sniper.enabled
            else settings.options_min_dte
        )
        if dte < minimum_dte or dte > settings.options_max_dte:
            rejected["dte"] += 1
            continue
        expires_near_earnings = (
            event_days is not None and event_days >= 0 and dte <= event_days + 1
        )
        direction_matches = item.option_type == preferred_side
        if item.feed.lower() != "opra" or settings.alpaca_options_feed.lower() != "opra":
            rejected["opra_unavailable"] += 1
            continue
        if item.bid is None or item.ask is None or item.bid <= 0 or item.ask < item.bid:
            rejected["invalid_quote"] += 1
            continue
        mid = (item.bid + item.ask) / 2
        spread = item.ask - item.bid
        spread_pct = spread / mid * 100
        maximum_spread = (
            min(settings.options_max_spread_pct, 6.0)
            if sniper.enabled and dte == 0
            else settings.options_max_spread_pct
        )
        if spread_pct > maximum_spread:
            rejected["wide_spread"] += 1
            continue
        volume, oi = int(item.volume or 0), int(item.open_interest or 0)
        greek_values = (item.delta, item.gamma, item.theta, item.vega, item.iv)
        if any(value is None for value in greek_values):
            rejected["missing_greeks"] += 1
            continue
        delta = float(item.delta)
        preferred_delta_min = (
            0.45 if sniper.enabled and dte == 0
            else 0.40 if sniper.enabled and dte <= 2
            else settings.options_min_abs_delta
        )
        preferred_delta_max = (
            0.65 if sniper.enabled and dte <= 2
            else settings.options_max_abs_delta
        )
        delta_in_range = preferred_delta_min <= abs(delta) <= preferred_delta_max
        quote_age = _age_seconds(item.quote_timestamp, generated)
        maximum_quote_age = (
            min(settings.options_max_quote_age_seconds, 10)
            if sniper.enabled and dte == 0
            else settings.options_max_quote_age_seconds
        )
        if quote_age > maximum_quote_age:
            rejected["stale_quote"] += 1
            continue
        moneyness, distance_pct, otm_distance = _moneyness(
            item.option_type, item.strike, underlying
        )
        if otm_distance > settings.options_max_otm_pct:
            rejected["deep_otm"] += 1
            continue

        gamma, theta, vega, iv = (
            float(item.gamma), float(item.theta), float(item.vega), float(item.iv)
        )
        entry = round(min(item.ask, mid + spread * 0.1), 2)
        contract_cost = round(entry * 100, 2)
        budget_fit = contract_cost <= settings.options_max_contract_cost_usd
        if sniper.enabled and not budget_fit:
            rejected["over_budget"] += 1
            continue
        capital_pct = contract_cost / capital_usd * 100 if capital_usd else 100

        expected_days = (
            max(0.05, min(0.5, max(dte, 0.25) / 6))
            if sniper.enabled and dte <= 2
            else max(0.5, min(3.0, dte / 6))
        )
        target_conservative = targets[0]
        target_base = targets[min(1, len(targets) - 1)]
        target_optimistic = targets[-1]
        scenarios = [
            _scenario(
                name="conservative", label_ar="متحفظ", target=target_conservative,
                underlying=underlying, entry=entry, delta=delta, gamma=gamma,
                theta=theta, vega=vega, iv_change_pct=-10, expected_days=expected_days + 1,
                spread=spread,
            ),
            _scenario(
                name="base", label_ar="أساسي", target=target_base,
                underlying=underlying, entry=entry, delta=delta, gamma=gamma,
                theta=theta, vega=vega, iv_change_pct=0, expected_days=expected_days,
                spread=spread,
            ),
            _scenario(
                name="optimistic", label_ar="متفائل", target=target_optimistic,
                underlying=underlying, entry=entry, delta=delta, gamma=gamma,
                theta=theta, vega=vega, iv_change_pct=5,
                expected_days=max(0.5, expected_days - 0.5), spread=spread,
            ),
        ]
        favorable_payoff = scenarios[1].estimated_contract_price > entry

        premium_loss_pct = min(
            settings.options_max_premium_loss_pct, max(18.0, 20.0 + iv * 15)
        )
        stop = round(max(0.01, entry * (1 - premium_loss_pct / 100)), 2)
        loss_per_contract = max(0.01, (entry - stop) * 100)
        profit_1 = max(0, scenarios[0].profit_usd)
        profit_2 = max(0, scenarios[1].profit_usd)
        rr = round(profit_2 / loss_per_contract, 2)

        delta_score = _clamp(100 - abs(abs(delta) - 0.5) * 300)
        strike_score = _clamp(100 - abs(distance_pct) * 10 + (8 if moneyness == "ITM" else 0))
        dte_score = (
            _clamp(100 - dte * 12)
            if sniper.enabled
            else _clamp(100 - abs(dte - 18) * 4)
        )
        spread_score = _clamp(100 - spread_pct * 5)
        volume_score = _clamp(30 + min(volume, 1000) / 14)
        oi_score = _clamp(30 + min(oi, 5000) / 70)
        theta_score = _clamp(100 - abs(theta / max(entry, 0.01)) * 500)
        iv_score = _clamp(100 - max(0, iv - 0.45) * 100)
        break_even = (
            item.strike + entry if item.option_type == OptionType.CALL else item.strike - entry
        )
        required_move_pct = round(abs(break_even - underlying) / underlying * 100, 2)
        cost_score = _clamp(
            100
            - max(
                0,
                contract_cost - settings.options_preferred_contract_cost_usd,
            )
            / max(settings.options_preferred_contract_cost_usd, 1)
            * 55
        )
        target_for_break_even = target_base
        break_even_score = _clamp(
            100 - abs(break_even - target_for_break_even) / underlying * 300
        )
        liquidity = _clamp(
            spread_score * 0.45 + volume_score * 0.25 + oi_score * 0.30
        )
        risk = _clamp(
            100 - (
                spread_score * 0.25 + theta_score * 0.20 + iv_score * 0.20
                + earnings_score * 0.20 + min(100, rr * 35) * 0.15
            ) + news_penalty
        )
        affordability_score = _clamp(
            100 - max(0, capital_pct - settings.options_max_capital_pct) * 1.5
        )
        rr_score = _clamp(rr * 35)
        stock_quality = _clamp(
            float(
                stock_analysis.get("overall_score")
                or stock_analysis.get("confidence_score")
                or 70
            )
        )
        indicator_values = stock_analysis.get("indicators") or {}
        relative_volume = float(
            indicator_values.get("relative_volume")
            or stock_analysis.get("relative_volume")
            or 1
        )
        momentum_quality = _clamp(
            45
            + min(30, max(0, relative_volume - 0.5) * 20)
            + (15 if direction_matches else 0)
        )
        news_score = _clamp(80 - news_penalty)
        market_sector_score = _clamp(75 if direction_matches else 45)
        contract_fit = _clamp(
            delta_score * 0.35
            + strike_score * 0.30
            + dte_score * 0.20
            + theta_score * 0.15
        )
        if expires_near_earnings:
            earnings_score = min(earnings_score, 25)
        components = {
            "stock_movement_quality": stock_quality,
            "momentum_relative_strength": momentum_quality,
            "news_catalyst": news_score,
            "market_sector_alignment": market_sector_score,
            "contract_liquidity": liquidity,
            "contract_fit": contract_fit,
            "risk_reward": rr_score,
            "direction": 100 if direction_matches else 35,
            "strike": strike_score,
            "delta": delta_score,
            "dte": dte_score,
            "spread": spread_score,
            "volume": volume_score,
            "open_interest": oi_score,
            "theta": theta_score,
            "iv": iv_score,
            "break_even": break_even_score,
            "liquidity": liquidity,
            "data_quality": 100,
            "earnings": earnings_score,
            "risk_reward": rr_score,
            "capital_fit": affordability_score,
            "budget_fit": cost_score,
            "news": 100 - news_penalty,
        }
        score = round(
            stock_quality * 0.25
            + momentum_quality * 0.15
            + news_score * 0.10
            + market_sector_score * 0.10
            + liquidity * 0.15
            + contract_fit * 0.15
            + rr_score * 0.10,
            2,
        )
        if sniper.enabled:
            score = round(
                score * 0.45
                + strike_score * 0.20
                + cost_score * 0.15
                + delta_score * 0.10
                + _clamp(100 - quote_age * 5) * 0.10,
                2,
            )
        intrinsic = max(
            0,
            underlying - item.strike
            if item.option_type == OptionType.CALL
            else item.strike - underlying,
        )
        warnings = []
        if iv_crush:
            warnings.append("تحذير IV Crush حول موعد الأرباح القادم.")
        if news_penalty:
            warnings.append("خبر حديث مرتفع التأثير خفّض درجة ملاءمة العقد ورفع مخاطره.")
        if volume < settings.options_min_volume:
            warnings.append(
                "حجم تداول العقد منخفض؛ خُفّضت درجة السيولة دون رفضه."
            )
        if oi < settings.options_min_open_interest:
            warnings.append(
                "Open Interest منخفض؛ العقد للمراقبة ويحتاج إعادة تحقق."
            )
        if not delta_in_range:
            warnings.append(
                "Delta خارج النطاق المفضل؛ خُفّضت درجة الملاءمة."
            )
        if not direction_matches:
            warnings.append(
                "نوع العقد لا يطابق الاتجاه الرئيسي؛ للمراقبة فقط."
            )
        if expires_near_earnings:
            warnings.append(
                "الانتهاء قريب من إعلان الأرباح؛ مخاطرة IV Crush مرتفعة."
            )
        if not favorable_payoff:
            warnings.append(
                "العائد النظري الأساسي غير مواتٍ حاليًا؛ لا دخول."
            )
        if capital_pct > settings.options_max_capital_pct:
            warnings.append(
                "تكلفة العقد أعلى من حد رأس المال المحدد؛ للمراقبة فقط."
            )
        time_remaining = sniper.time_remaining_minutes(item, generated)
        eastern_minutes = (
            session.new_york_time.hour * 60 + session.new_york_time.minute
        )
        first_five_minutes = 9 * 60 + 30 <= eastern_minutes < 9 * 60 + 35
        near_close_without_momentum = (
            sniper.enabled
            and dte == 0
            and time_remaining < 30
            and (stock_quality < 80 or relative_volume < 1.5)
        )
        if sniper.enabled and dte == 0:
            warnings.append("عقود 0DTE قد تفقد معظم قيمتها خلال دقائق.")
        if sniper.enabled and first_five_minutes:
            warnings.append(
                "أول 5 دقائق للمراقبة فقط؛ انتظر تأكيد Opening Range."
            )
        if near_close_without_momentum:
            warnings.append(
                "الوقت المتبقي قصير والزخم غير كافٍ؛ 0DTE غير قابل للدخول."
            )
        if not session.options_actionable:
            warnings.append(
                "التحليل للمراقبة فقط، ولا يمكن تنفيذ العقد حتى افتتاح سوق الخيارات."
            )
        badges = (
            ["السوق مفتوح", "جاهز عند تحقق الشرط"]
            if session.options_actionable
            else ["السوق مغلق", "قابل للمراقبة"]
        )
        if spread_pct > settings.options_max_spread_pct * 0.7:
            badges.append("قريب من حد السبريد")
        trade_age = (
            _age_seconds(item.trade_timestamp, generated) if item.trade_timestamp else None
        )
        safe_for_entry = (
            session.options_actionable
            and direction_matches
            and delta_in_range
            and favorable_payoff
            and not expires_near_earnings
            and capital_pct <= settings.options_max_capital_pct
            and volume >= settings.options_min_volume
            and oi >= settings.options_min_open_interest
            and budget_fit
            and not (sniper.enabled and first_five_minutes)
            and not near_close_without_momentum
        )
        badges = (
            ["السوق مفتوح", "جاهز عند تحقق الشرط"]
            if safe_for_entry
            else ["السوق مفتوح", "للمراقبة فقط"]
            if session.options_actionable
            else ["السوق مغلق", "قابل للمراقبة"]
        )
        classification = (
            "فرصة قوية"
            if safe_for_entry and score >= 80
            else "قنص مشروط"
            if safe_for_entry and score >= 68
            else "العقد رخيص لكنه ضعيف"
            if (
                sniper.enabled
                and contract_cost <= settings.options_preferred_contract_cost_usd
                and score < 50
            )
            else "للمراقبة"
            if score >= 50
            else "انتظر"
        )
        selection_reason = (
            f"{classification}: درجة {score:.0f}/100؛ "
            f"سيولة {liquidity}/100 وملاءمة {contract_fit}/100."
        )
        badges.append(classification)
        ranked.append(
            RankedOptionContract(
                symbol=item.symbol,
                underlying_symbol=base["symbol"],
                option_type=item.option_type,
                strike=item.strike,
                expiration=item.expiration,
                dte=dte,
                bid=round(item.bid, 2),
                ask=round(item.ask, 2),
                mid=round(mid, 2),
                last=round(item.last, 2) if item.last is not None else None,
                spread=round(spread, 2),
                spread_pct=round(spread_pct, 2),
                volume=volume,
                open_interest=oi,
                delta=round(delta, 4),
                gamma=round(gamma, 4),
                theta=round(theta, 4),
                vega=round(vega, 4),
                iv=round(iv, 4),
                underlying_price=round(underlying, 2),
                intrinsic_value=round(intrinsic, 2),
                extrinsic_value=round(max(0, entry - intrinsic), 2),
                break_even=round(break_even, 2),
                distance_to_strike_pct=distance_pct,
                moneyness=moneyness,
                volume_oi_ratio=round(volume / oi, 4) if oi else 0,
                entry_price=entry,
                contract_cost=contract_cost,
                suitability_score=_clamp(score),
                liquidity_score=liquidity,
                risk_score=risk,
                ranking_score=score,
                ranking_components=components,
                target_1=scenarios[0].estimated_contract_price,
                target_2=scenarios[1].estimated_contract_price,
                target_3=scenarios[2].estimated_contract_price,
                stop_loss=stop,
                premium_loss_pct=round(premium_loss_pct, 2),
                expected_profit_target_1_pct=scenarios[0].profit_pct,
                expected_profit_target_2_pct=scenarios[1].profit_pct,
                risk_reward=rr,
                stock_entry=stock_entry,
                stock_stop=stock_stop,
                stock_targets=targets,
                target_scenarios=scenarios,
                quote_timestamp=item.quote_timestamp,
                trade_timestamp=item.trade_timestamp,
                quote_age_seconds=quote_age,
                trade_age_seconds=trade_age,
                feed="opra",
                actionable=safe_for_entry,
                status_badges_ar=badges,
                entry_instruction_ar=(
                    "دخول ورقي مشروط بعد تحقق دخول السهم وإعادة فحص Bid/Ask."
                    if safe_for_entry
                    else "دخول مشروط بعد افتتاح السوق وإعادة التحقق من Bid/Ask."
                    if not session.options_actionable
                    else "للمراقبة فقط؛ أعد التحقق من الشروط وBid/Ask قبل أي دخول ورقي."
                ),
                exit_conditions_ar=[
                    "كسر السهم مستوى الإبطال الفني.",
                    f"هبوط Premium إلى {stop:.2f} أو خسارة {premium_loss_pct:.1f}%.",
                    "انتهاء صلاحية التحليل.",
                    "تقادم بيانات السهم أو العقد.",
                    "اتساع السبريد بشدة.",
                    "ظهور مخاطرة خبرية جديدة.",
                    (
                        f"Time Stop بعد {5 if dte == 0 else 10 if dte <= 2 else 15} دقائق "
                        "إذا لم تتحقق الحركة المتوقعة."
                        if sniper.enabled
                        else "إعادة التقييم إذا لم تتحقق الحركة خلال الوقت المتوقع."
                    ),
                ],
                valid_for_minutes=(
                    min(valid_minutes, 5 if dte == 0 else 10 if dte <= 2 else 15)
                    if sniper.enabled
                    else valid_minutes
                ),
                expires_at=expires_at,
                warnings_ar=warnings,
                classification_ar=classification,
                selection_reason_ar=selection_reason,
                risk_notes_ar=list(warnings),
                budget_fit=budget_fit,
                required_move_pct=required_move_pct,
                time_remaining_minutes=time_remaining,
                time_stop_minutes=(
                    5 if dte == 0 else 10 if dte <= 2 else 15
                    if sniper.enabled
                    else None
                ),
            )
        )

    ranked.sort(key=lambda value: value.ranking_score, reverse=True)
    scalp_stage = "standard"
    scalp_stage_label = "7–30 DTE"
    if sniper.enabled:
        stages = (
            (
                "primary",
                "0–2 DTE",
                settings.options_scalp_min_dte,
                settings.options_scalp_max_dte,
            ),
            (
                "short_fallback",
                "احتياط 3–7 DTE",
                settings.options_scalp_max_dte + 1,
                settings.options_scalp_fallback_max_dte,
            ),
            (
                "standard_fallback",
                "احتياط 7–30 DTE",
                max(settings.options_min_dte, settings.options_scalp_fallback_max_dte),
                settings.options_max_dte,
            ),
        )
        selected_stage: list[RankedOptionContract] = []
        for name, label, minimum, maximum in stages:
            selected_stage = [
                item
                for item in ranked
                if minimum <= item.dte <= maximum
                and item.option_type == preferred_side
            ]
            if selected_stage:
                scalp_stage, scalp_stage_label = name, label
                break
        shortlisted, scalp_modes = sniper.choose_modes(selected_stage)
    else:
        shortlisted = ranked[: max(1, min(3, settings.options_contract_limit))]
        scalp_modes = {}
    best_call = next((item for item in shortlisted if item.option_type == OptionType.CALL), None)
    best_put = next((item for item in shortlisted if item.option_type == OptionType.PUT), None)
    warnings = ["Paper Trading فقط — لا يوجد تنفيذ تلقائي أو أوامر Live."]
    if sniper.enabled and scalp_stage != "primary" and shortlisted:
        warnings.append(
            f"لم توجد عقود 0–2 DTE بجودة كافية؛ تم التوسع إلى {scalp_stage_label}."
        )
    if sniper.enabled and not shortlisted and rejected.get("over_budget"):
        warnings.append(
            "لا يوجد عقد قريب من السترايك ومناسب للميزانية بجودة كافية."
        )
    if sniper.enabled and any(item.dte == 0 for item in shortlisted):
        warnings.append("عقود 0DTE قد تفقد معظم قيمتها خلال دقائق.")
    if not session.options_actionable:
        warnings.extend([
            "التحليل للمراقبة فقط، ولا يمكن تنفيذ العقد حتى افتتاح سوق الخيارات.",
            "هذه قراءة مبدئية، ويجب إعادة تسعير العقد بعد افتتاح سوق الخيارات.",
        ])
    if iv_crush:
        warnings.append("موعد الأرباح قريب؛ تقلب IV قد يسبب IV Crush وفجوة سعرية.")
    reject_before_earnings = earnings_risk == "very_high"
    earnings_option_fit = (
        "مرفوض قبل الأرباح لقرب الحدث"
        if reject_before_earnings
        else "مناسب للمراقبة فقط قبل الأرباح مع تحذير IV Crush"
        if iv_crush
        else "لا يوجد تحذير أرباح قريب"
    )
    if not shortlisted and rejected.get("stale_quote"):
        base["market"]["options_status"] = "stale"
        base["market"]["options_label_ar"] = "بيانات قديمة"
    elif not shortlisted and rejected.get("opra_unavailable"):
        base["market"]["options_status"] = "opra_unavailable"
        base["market"]["options_label_ar"] = "بيانات OPRA غير متاحة"
    scalp_decision = (
        "قنص مشروط"
        if any(item.actionable for item in shortlisted)
        else "للمراقبة"
        if shortlisted
        else "لا يوجد عقد مناسب للميزانية"
        if rejected.get("over_budget")
        else "لا صفقة"
    )
    scalp_summary = (
        {
            "enabled": True,
            "engine": "ShortDTEOptionSniper",
            "decision_ar": scalp_decision,
            "strategy": sniper.strategy_name(stock_analysis, generated),
            "dte_stage": scalp_stage,
            "dte_stage_label_ar": scalp_stage_label,
            "allowed_strikes": list(sniper_universe.allowed_strikes),
            "atm_strike": sniper_universe.atm_strike,
            "confirmation_level": stock_entry,
            "confirmation_ar": (
                (stock_analysis.get("strategy") or {}).get("trigger")
                or f"الدخول بعد تأكيد حركة الأصل حول {stock_entry:.2f}."
            ),
            "invalidation_level": stock_stop,
            "modes": scalp_modes,
            "account_size_usd": settings.options_account_size_usd,
            "maximum_contract_cost_usd": settings.options_max_contract_cost_usd,
            "preferred_contract_cost_usd": (
                settings.options_preferred_contract_cost_usd
            ),
            "rejected_cheaper_reasons": {
                key: value
                for key, value in rejected.items()
                if key
                in {
                    "invalid_quote",
                    "wide_spread",
                    "missing_greeks",
                    "stale_quote",
                    "deep_otm",
                    "over_budget",
                }
            },
        }
        if sniper.enabled
        else {}
    )
    return OptionChainResult(
        status=(
            "ready"
            if any(item.actionable for item in shortlisted)
            else "monitoring"
            if shortlisted
            else "no_contract"
        ),
        **base,
        contracts_considered=len(contracts),
        contracts_rejected=sum(rejected.values()),
        best_call=best_call,
        best_put=best_put,
        ranked_contracts=shortlisted,
        rejection_reasons=dict(rejected),
        earnings_implied_move_pct=implied_move_pct,
        earnings_current_iv=earnings_iv,
        earnings_option_fit_ar=earnings_option_fit,
        reject_before_earnings=reject_before_earnings,
        warnings_ar=warnings,
        scalp_summary=scalp_summary,
    )
