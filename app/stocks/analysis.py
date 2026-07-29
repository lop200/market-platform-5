from __future__ import annotations

from datetime import datetime, timedelta, timezone
from math import isfinite

import pandas as pd
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.models import NewsItem, UserRiskSettings
from app.opportunities.indicators import calculate_indicators
from app.opportunities.market_regime import classify_market, current_session
from app.opportunities.news import get_news_provider
from app.opportunities.risk import position_size, risk_reward
from app.opportunities.schemas import MarketRegime
from app.opportunities.strategies import select_strategy
from app.providers.base import MarketDataAdapter, Quote
from app.stocks.quality import evaluate_plan_data


def _safe_number(value, digits: int = 4):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return round(number, digits) if isfinite(number) else None


def _bars(frame: pd.DataFrame | None, limit: int = 240) -> list[dict]:
    if frame is None or frame.empty:
        return []
    work = frame.copy()
    close = work["close"].astype(float)
    work["ema9"] = close.ewm(span=9, adjust=False).mean()
    work["ema20"] = close.ewm(span=20, adjust=False).mean()
    work["ema50"] = close.ewm(span=50, adjust=False).mean()
    typical = (work["high"] + work["low"] + work["close"]) / 3
    cumulative_volume = work["volume"].astype(float).cumsum()
    work["vwap"] = (typical * work["volume"]).cumsum() / cumulative_volume.replace(0, pd.NA)
    result = []
    for index, row in work.tail(limit).iterrows():
        timestamp = index.isoformat() if hasattr(index, "isoformat") else str(index)
        result.append({
            "time": timestamp,
            "open": _safe_number(row.get("open")),
            "high": _safe_number(row.get("high")),
            "low": _safe_number(row.get("low")),
            "close": _safe_number(row.get("close")),
            "volume": int(row.get("volume") or 0),
            "vwap": _safe_number(row.get("vwap")),
            "ema9": _safe_number(row.get("ema9")),
            "ema20": _safe_number(row.get("ema20")),
            "ema50": _safe_number(row.get("ema50")),
        })
    return result


def _risk_settings(db: Session, settings: Settings) -> UserRiskSettings:
    row = db.get(UserRiskSettings, 1)
    if row is None:
        row = UserRiskSettings(
            id=1,
            capital_sar=settings.default_capital_sar,
            max_risk_pct=settings.default_risk_pct,
            max_open_positions=settings.max_open_positions,
            daily_loss_limit_pct=settings.default_daily_loss_pct,
            currency="SAR",
        )
        db.add(row)
        db.flush()
    return row


def _trend(indicators: dict) -> str:
    ema9 = indicators.get("ema9")
    ema20 = indicators.get("ema20")
    ema50 = indicators.get("ema50")
    if all(value is not None for value in (ema9, ema20, ema50)):
        if ema9 > ema20 > ema50:
            return "صاعد"
        if ema9 < ema20 < ema50:
            return "هابط"
    return "جانبي"


def _timeframe_reading(alignment: dict[str, str | None]) -> str:
    short_up = alignment.get("1m") == "صاعد" and alignment.get("5m") == "صاعد"
    short_down = alignment.get("1m") == "هابط" and alignment.get("5m") == "هابط"
    daily = alignment.get("daily")
    if short_up and daily == "هابط":
        return "ارتداد لحظي صاعد داخل اتجاه يومي هابط — مخاطرة أعلى."
    if short_down and daily == "صاعد":
        return "هبوط لحظي داخل اتجاه يومي صاعد — تعارض فريمات."
    values = [value for value in alignment.values() if value]
    if values and all(value == "صاعد" for value in values):
        return "توافق صاعد متعدد الفريمات."
    if values and all(value == "هابط" for value in values):
        return "توافق هابط متعدد الفريمات."
    if len(set(values)) > 1:
        return "تضارب فريمات — لا يوجد اتجاه تنفيذي واضح."
    return "اتجاه جانبي أو بيانات أطر غير مكتملة."


def _probabilities(indicators: dict, regime: MarketRegime, rr: float | None) -> dict:
    required = ("relative_volume", "atr", "volatility", "ema9", "ema20", "ema50")
    if sum(indicators.get(key) is not None for key in required) < 5:
        return {
            "entry": "متوسطة",
            "target_1": "متوسطة",
            "target_2": "منخفضة",
            "stop": "متوسطة",
            "confidence": "منخفضة",
            "numeric": False,
        }
    rv = min(max(float(indicators.get("relative_volume") or 0), 0), 3)
    aligned = 1 if _trend(indicators) == "صاعد" else 0.45
    market = 1 if regime == MarketRegime.BULLISH else 0.7 if regime == MarketRegime.CHOPPY else 0.45
    base = max(25, min(78, 32 + rv * 9 + aligned * 15 + market * 10))
    target1 = max(18, min(72, base - 5 + min(float(rr or 0), 3) * 2))
    target2 = max(10, min(58, target1 - 15))
    stop = max(15, min(65, 72 - base / 2))
    confidence = "مرتفعة" if base >= 68 else "متوسطة" if base >= 48 else "منخفضة"
    return {
        "entry": round(base),
        "target_1": round(target1),
        "target_2": round(target2),
        "stop": round(stop),
        "confidence": confidence,
        "numeric": True,
    }


def _time_estimate(distance_pct: float | None, volatility: float | None, atr_pct: float | None) -> dict:
    movement = max(float(volatility or 0), float(atr_pct or 0), 0.1)
    ratio = float(distance_pct or 0) / movement
    if ratio <= 0.8:
        base = "خلال 30–90 دقيقة"
        fastest = "خلال 30 دقيقة عند تسارع الحجم"
    elif ratio <= 1.8:
        base = "خلال الجلسة"
        fastest = "خلال 90 دقيقة عند استمرار الزخم"
    elif ratio <= 3:
        base = "خلال يوم تداول"
        fastest = "خلال الجلسة في حال اتساع الحركة"
    else:
        base = "خلال يومين إلى ثلاثة أيام"
        fastest = "خلال يوم تداول عند ارتفاع التذبذب"
    return {
        "expected": base,
        "fastest": fastest,
        "base_case": base,
        "invalid_when": "يفقد التقدير صلاحيته عند كسر مستوى الإبطال أو هبوط الحجم أو اتساع السبريد.",
    }


def _market_session_label(session: str | None = None) -> str:
    if session:
        direct = {
            "pre_market": "قبل السوق",
            "regular": "السوق مفتوح",
            "after_hours": "بعد السوق",
            "closed": "السوق مغلق",
        }.get(session)
        if direct:
            return direct
    return {
        "pre_market": "قبل السوق",
        "open": "مفتوح — الافتتاح",
        "mid_session": "مفتوح",
        "close": "مفتوح — قرب الإغلاق",
        "after_hours": "بعد السوق",
    }.get(current_session(), "مغلق")


def analyze_single_stock(
    db: Session,
    provider: MarketDataAdapter,
    settings: Settings,
    symbol: str,
) -> dict:
    """Analyze one symbol without scanner price/liquidity eligibility filters."""
    started = datetime.now(timezone.utc)
    before = provider.telemetry_snapshot()
    warnings: list[str] = []
    missing: list[str] = []

    quote: Quote | None = None
    try:
        quote = provider.get_quote(symbol)
    except Exception:
        missing.append("تعذر جلب السعر وBid/Ask")

    daily = None
    frames: dict[str, pd.DataFrame | None] = {}
    try:
        daily = provider.get_daily_ohlcv(symbol, 240)
        frames["1d"] = daily
    except Exception:
        missing.append("الشموع اليومية غير متوفرة")
    for interval in ("1m", "5m", "15m", "1h"):
        try:
            frames[interval] = provider.get_intraday(symbol, interval)
            if frames[interval] is None or frames[interval].empty:
                missing.append(f"شموع {interval} غير متوفرة")
        except Exception:
            frames[interval] = None
            missing.append(f"شموع {interval} غير متوفرة")

    try:
        profile = provider.get_company_profile(symbol)
    except Exception:
        profile = {}
        warnings.append("بيانات الشركة الأساسية غير متوفرة")

    try:
        regime, regime_inputs = classify_market(provider)
    except Exception:
        regime, regime_inputs = MarketRegime.HIGH_RISK, {}
        warnings.append("تعذر اكتمال قراءة السوق العامة")

    indicators: dict = {}
    primary = frames.get("5m")
    if daily is not None and primary is not None and len(daily) >= 20 and len(primary) >= 20:
        indicators = calculate_indicators(daily, primary)
        for interval in ("1m", "15m", "1h"):
            frame = frames.get(interval)
            if frame is not None and len(frame) >= 20:
                closes = frame["close"].astype(float)
                indicators[f"momentum_{interval}"] = _safe_number(closes.pct_change(3).iloc[-1] * 100)
                indicators[f"trend_{interval}"] = (
                    "صاعد" if closes.ewm(span=9, adjust=False).mean().iloc[-1]
                    > closes.ewm(span=20, adjust=False).mean().iloc[-1] else "هابط"
                )
        if daily is not None and len(daily) >= 50:
            daily_close = daily["close"].astype(float)
            indicators["trend_daily"] = (
                "صاعد" if daily_close.ewm(span=20, adjust=False).mean().iloc[-1]
                > daily_close.ewm(span=50, adjust=False).mean().iloc[-1] else "هابط"
            )
    else:
        missing.append("المؤشرات الكاملة تحتاج 20 شمعة يومية و20 شمعة 5 دقائق على الأقل")

    price = float(quote.price) if quote else (
        float(primary["close"].iloc[-1]) if primary is not None and not primary.empty else
        float(daily["close"].iloc[-1]) if daily is not None and not daily.empty else None
    )
    previous_close = float(daily["close"].iloc[-2]) if daily is not None and len(daily) >= 2 else None
    change_pct = ((price / previous_close) - 1) * 100 if price and previous_close else None
    average_volume = indicators.get("average_volume")
    volume = quote.volume if quote and quote.volume is not None else (
        int(primary["volume"].sum()) if primary is not None and not primary.empty else None
    )
    dollar_volume = float(price * volume) if price and volume else None

    news_items = []
    verified_recent_news = False
    try:
        news = get_news_provider(settings).get_news(symbol)
        for item in news[:6]:
            age_hours = max(0, int((started - item.published_at).total_seconds() / 3600))
            verified_recent_news = verified_recent_news or (
                item.is_official and age_hours <= 24 and item.classification not in {"قديم", "غير موثوق", "ترويجي"}
            )
            news_items.append({
                "headline": item.headline, "source": item.source,
                "published_at": item.published_at.isoformat(), "age": f"قبل {age_hours} ساعة",
                "summary_ar": item.headline, "impact": item.classification,
                "official": item.is_official, "risk_flags": item.risk_flags,
            })
            db.add(NewsItem(
                symbol=symbol, headline=item.headline, source=item.source,
                published_at=item.published_at, url=item.url,
                classification=item.classification, is_official=item.is_official,
                risk_flags=item.risk_flags,
            ))
    except Exception:
        warnings.append("تعذر مزود الأخبار، واكتمل التحليل الفني دون أخبار")

    try:
        market_open = provider.is_market_open() or bool(quote and quote.session == "pre_market")
    except Exception:
        market_open = current_session() in {"open", "mid_session", "close"}
        warnings.append("تعذر التحقق المباشر من ساعة السوق")
    quality = evaluate_plan_data(quote, primary, settings, market_open=market_open)
    warnings.extend(quality.warnings)
    warnings.extend(quality.reasons)
    strategy = (
        select_strategy(indicators, price or 0, regime, verified_news=verified_recent_news)
        if indicators and price else None
    )
    status = "no_trade"
    status_ar = "لا توجد نقطة دخول مناسبة حاليًا"
    entry_from = entry_to = stop = rr = None
    targets: list[dict] = []
    if quote and quote.ask and price and indicators:
        atr = max(float(indicators.get("atr") or price * .02), price * .005)
        support = float(indicators.get("support") or price - atr)
        resistance = float(indicators.get("resistance") or price + atr)
        entry_from = round(max(float(quote.ask), resistance + .01), 2) if strategy and strategy.strategy_id in {
            "volume_breakout", "opening_range_breakout"
        } else round(max(float(quote.ask), price), 2)
        stop = round(max(.01, min(support - .01, entry_from - atr * .8)), 2)
        if entry_from > stop:
            risk = entry_from - stop
            target_prices = [
                round(entry_from + risk * settings.min_risk_reward, 2),
                round(entry_from + risk * (settings.min_risk_reward + 1), 2),
            ]
            resistance_2 = round(max(target_prices[-1], resistance + atr), 2)
            if resistance_2 > target_prices[-1] * 1.01:
                target_prices.append(resistance_2)
            entry_to = round(entry_from + min(atr * .2, entry_from * .01), 2)
            rr = risk_reward(entry_from, stop, target_prices[0])
            targets = [{
                "price": target,
                "label": f"المستوى {index}",
                "profit_pct": round((target / entry_from - 1) * 100, 2),
            } for index, target in enumerate(target_prices, 1)]
            if strategy and strategy.strategy_id != "no_trade" and quality.valid_for_plan:
                status = "conditional_entry"
                status_ar = "دخول مشروط"

    if status != "conditional_entry":
        entry_from = entry_to = stop = rr = None
        targets = []

    stop_distance_pct = round((entry_from - stop) / entry_from * 100, 2) if entry_from and stop else None
    target_distance = targets[0]["profit_pct"] if targets else None
    atr_pct = float(indicators.get("atr") or 0) / price * 100 if price else None
    probabilities = _probabilities(indicators, regime, rr) if status == "conditional_entry" else None
    time_estimate = (
        _time_estimate(target_distance, indicators.get("volatility"), atr_pct)
        if status == "conditional_entry" else None
    )
    alignment = {
        "1m": indicators.get("trend_1m"),
        "5m": _trend(indicators),
        "15m": indicators.get("trend_15m"),
        "1h": indicators.get("trend_1h"),
        "daily": indicators.get("trend_daily"),
    }
    trend = _timeframe_reading(alignment)

    risk_row = _risk_settings(db, settings)
    plan = None
    if status == "conditional_entry" and entry_from and stop and targets:
        sized = position_size(
            float(risk_row.capital_sar), float(risk_row.max_risk_pct),
            entry_from, stop, [item["price"] for item in targets], settings.usd_sar_rate,
        )
        plan = {
            "shares": sized.shares,
            "position_value_usd": sized.position_value_usd,
            "max_loss_sar": sized.max_loss_sar,
            "capital_used_pct": sized.capital_used_pct,
            "estimated_profit_sar": sized.estimated_profit_sar,
        }

    after = provider.telemetry_snapshot()
    response_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
    technical_levels = {
        "vwap": indicators.get("vwap"),
        "ema9": indicators.get("ema9"),
        "ema20": indicators.get("ema20"),
        "ema50": indicators.get("ema50"),
        "support": indicators.get("support"),
        "resistance": indicators.get("resistance"),
        "entry_from": entry_from,
        "entry_to": entry_to,
        "stop": stop,
        "targets": [item["price"] for item in targets],
    }
    valid_minutes = strategy.valid_minutes if strategy and status == "conditional_entry" else 0
    expires_at = started + timedelta(minutes=valid_minutes) if valid_minutes else started
    result = {
        "symbol": symbol,
        "company_name": profile.get("name") or symbol,
        "quote": {
            "price": _safe_number(price),
            "bid": _safe_number(quote.bid) if quote else None,
            "ask": _safe_number(quote.ask) if quote else None,
            "mid": _safe_number(quote.mid) if quote else None,
            "spread": _safe_number(quote.spread) if quote else None,
            "spread_pct": _safe_number(quote.spread_pct, 2) if quote else None,
            "change_pct": _safe_number(change_pct, 2),
            "volume": volume,
            "average_volume": _safe_number(average_volume, 0),
            "relative_volume": indicators.get("relative_volume"),
            "dollar_volume": _safe_number(dollar_volume, 0),
            "volatility": indicators.get("volatility"),
            "market_cap": profile.get("market_cap"),
            "float_shares": profile.get("float_shares"),
            "updated_at": quote.as_of if quote else None,
            "age_seconds": quote.age_seconds if quote else None,
            "trade_timestamp": quote.trade_as_of or quote.as_of if quote else None,
            "quote_timestamp": quote.bid_as_of or quote.ask_as_of or quote.as_of if quote else None,
            "bid_timestamp": quote.bid_as_of or quote.as_of if quote else None,
            "ask_timestamp": quote.ask_as_of or quote.as_of if quote else None,
            "bar_timestamp": quote.bar_as_of if quote else None,
            "price_source": quote.price_source if quote else "historical_bar",
            "trade_age_seconds": quality.trade_age_seconds,
            "bid_age_seconds": quality.bid_age_seconds,
            "ask_age_seconds": quality.ask_age_seconds,
            "last_candle_timestamp": (
                quote.bar_as_of if quote and quote.bar_as_of else
                primary.index[-1].isoformat() if primary is not None and not primary.empty else None
            ),
            "candle_age_seconds": quality.candle_age_seconds,
            "provider": quote.provider if quote else provider.provider_name,
            "feed": quote.feed if quote else None,
            "delayed": (quote.is_delayed or quote.age_seconds > settings.max_quote_age_seconds) if quote else True,
            "market_session": _market_session_label(quote.session if quote else None),
        },
        "market": {"regime": regime.value, "inputs": regime_inputs},
        "trend": trend,
        "analysis_type": "intraday" if status == "conditional_entry" else "no_setup",
        "generated_at": started.isoformat(),
        "expires_at": expires_at.isoformat(),
        "valid_for_minutes": valid_minutes,
        "is_expired": valid_minutes == 0,
        "data_quality": quality.as_dict(),
        "data_quality_message": (
            None if quality.valid_for_plan
            else "البيانات الحالية غير صالحة لبناء دخول أو وقف أو أهداف."
        ),
        "status": status,
        "status_ar": status_ar,
        "strategy": {
            "id": strategy.strategy_id if strategy and status == "conditional_entry" else "no_trade",
            "name_ar": strategy.name_ar if strategy and status == "conditional_entry" else "لا توجد خطة قابلة للتنفيذ",
            "name_en": strategy.name_en if strategy and status == "conditional_entry" else "No Trade",
            "reason": (
                strategy.reason if strategy and status == "conditional_entry"
                else "القراءة الحالية للمراقبة فقط بسبب جودة البيانات أو عدم اكتمال الشروط."
            ),
            "trigger": (
                strategy.trigger if strategy and status == "conditional_entry"
                else "انتظار Quote حديث ومتزامن وسوق مفتوح ثم إعادة التحليل."
            ),
        },
        "trade_plan": ({
            "entry_from": entry_from,
            "entry_to": entry_to,
            "stop": stop,
            "stop_reason": f"أسفل الدعم الفني؛ {strategy.invalidation}" if strategy else "لا يوجد مستوى إبطال مكتمل",
            "targets": targets,
            "risk_reward": rr,
            "stop_distance_pct": stop_distance_pct,
            "valid_minutes": valid_minutes,
            "expires_at": expires_at.isoformat(),
            "invalidation": [
                strategy.invalidation if strategy else "نقص البيانات",
                "هبوط الحجم",
                "اتساع السبريد",
                "خبر سلبي جوهري",
            ],
            "strengths": [strategy.reason] if strategy and strategy.strategy_id != "no_trade" else [],
            "risks": warnings[:2] + missing[:2],
            "position_size": plan,
        } if status == "conditional_entry" else None),
        "probabilities": probabilities,
        "probability_disclaimer": "تقدير احتمالي وليس ضمانًا أو نسبة نجاح تاريخية إلا عند ذكر ذلك صراحة.",
        "time_estimate": time_estimate,
        "scenarios": {
            "bullish": (
                f"مراقبة ثبات السعر فوق المقاومة {float(indicators.get('resistance')):.2f} "
                "مع تحسن الحجم والسبريد."
                if indicators.get("resistance") else "انتظار مقاومة واضحة وتأكيد حجم."
            ),
            "bearish": (
                f"كسر الدعم {float(indicators.get('support')):.2f} يبقي القراءة ضعيفة "
                "ويلغي أي سيناريو صاعد."
                if indicators.get("support") else "فقدان القاع الأخير يبقي المخاطر مرتفعة."
            ),
        },
        "indicators": indicators,
        "timeframe_alignment": alignment,
        "charts": {name: _bars(frame) for name, frame in frames.items()},
        "chart_levels": technical_levels,
        "news": news_items,
        "news_message": None if news_items else "لا توجد أخبار متاحة من مزود رسمي حاليًا.",
        "directional_bias": ({
            "label": (
                "ميل صاعد — يتوافق نظريًا مع Call" if trend == "صاعد" else
                "ميل هابط — يتوافق نظريًا مع Put" if trend == "هابط" else
                "محايد — لا يوجد انحياز واضح"
            ),
            "issued_at": started.isoformat(),
            "valid_minutes": valid_minutes,
            "warning": "هذا توصيف لاتجاه السهم فقط، وليس توصية بعقد أوبشن أو اختيار انتهاء أو سترايك.",
        } if quality.valid_for_plan and "تضارب" not in trend else None),
        "missing_data": list(dict.fromkeys(missing)),
        "warnings": list(dict.fromkeys(warnings)),
        "system_usage": {
            "symbols_requested": 1 + sum(value is not None for value in regime_inputs.values()),
            "api_requests": after["api_requests"] - before["api_requests"],
            "market_data_api_calls": after["api_requests"] - before["api_requests"],
            "cache_hits": after["cache_hits"] - before["cache_hits"],
            "provider": provider.provider_name,
            "response_ms": response_ms,
            "total_response_time_ms": response_ms,
            "openai_calls": 0,
            "openai_cost_usd": 0.0,
        },
        "ai_review": {
            "model_name": settings.openai_model,
            "prompt_version": None,
            "status": "pending" if quality.valid_for_plan else "skipped_invalid_data",
            "message_ar": (
                "بانتظار المراجعة الذكية" if quality.valid_for_plan
                else "لم تُرسل البيانات إلى OpenAI لأنها غير صالحة لبناء خطة."
            ),
            "ai_calls": 0,
            "ai_cost_estimate": 0.0,
            "ai_analysis_timestamp": None,
        },
    }
    db.commit()
    return result
