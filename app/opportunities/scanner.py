from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.models import (
    MarketRegimeRecord,
    NewsItem,
    OpportunityEvent,
    OpportunityTarget,
    StockCandidate,
    StockOpportunity,
    UserRiskSettings,
)
from app.opportunities.indicators import calculate_indicators
from app.opportunities.market_regime import classify_market, current_session
from app.opportunities.news import get_news_provider
from app.opportunities.openai_review import review_candidates
from app.opportunities.quality import evaluate_quote
from app.opportunities.risk import position_size, risk_reward
from app.opportunities.schemas import EntryZone, MarketRegime, OpportunityResult, OpportunityStatus, Target
from app.opportunities.strategies import select_strategy
from app.providers.base import MarketDataAdapter, Quote


def _quote_time(quote: Quote) -> datetime:
    value = datetime.fromisoformat(quote.as_of.replace("Z", "+00:00"))
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


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


def build_opportunity(
    db: Session,
    provider: MarketDataAdapter,
    settings: Settings,
    symbol: str,
    regime: MarketRegime,
    scan_run_id=None,
    quote_override: Quote | None = None,
    daily_override=None,
) -> tuple[OpportunityResult | None, list[str], dict]:
    quote = quote_override or provider.get_quote(symbol)
    quality = evaluate_quote(quote, settings)
    snapshot: dict = {
        "symbol": symbol,
        "price": quote.price,
        "bid": quote.bid,
        "ask": quote.ask,
        "spread_pct": quote.spread_pct,
        "quote_age_seconds": quote.age_seconds,
    }
    daily = daily_override if daily_override is not None else provider.get_daily_ohlcv(symbol, 220)
    intraday = provider.get_intraday(symbol, "5m")
    if intraday is None or len(intraday) < 20 or len(daily) < 20:
        return None, ["لا توجد شموع كافية للتحليل متعدد الأطر"], snapshot
    indicators = calculate_indicators(daily, intraday)
    for interval in ("1m", "15m"):
        try:
            frame = provider.get_intraday(symbol, interval)
            snapshot[f"bars_{interval}"] = len(frame) if frame is not None else 0
            if frame is not None and len(frame) >= 20:
                closes = frame["close"].astype(float)
                indicators[f"momentum_{interval}"] = round(float(closes.pct_change(3).iloc[-1] * 100), 4)
                if interval == "15m":
                    indicators["trend_15m_bullish"] = bool(
                        closes.ewm(span=9, adjust=False).mean().iloc[-1]
                        > closes.ewm(span=20, adjust=False).mean().iloc[-1]
                    )
        except Exception:
            quality.warnings.append(f"الإطار {interval} غير متوفر من المزود")
    snapshot["indicators"] = indicators
    previous_close = float(daily["close"].astype(float).iloc[-2]) if len(daily) >= 2 else quote.price
    snapshot.update({
        "stage": "analyzed",
        "change_pct": round((quote.price / previous_close - 1) * 100, 2) if previous_close else 0,
        "trend": (
            "صاعد" if (indicators.get("ema9") or 0) > (indicators.get("ema20") or 0)
            else "هابط"
        ),
        "liquidity": round(float(quote.price) * float(indicators.get("average_volume") or 0), 0),
        "volatility": indicators.get("volatility"),
        "support": indicators.get("support"),
        "resistance": indicators.get("resistance"),
        "watch_reason": "لم تكتمل شروط الدخول الفني الحالية",
        "activation_condition": "انتظار تأكيد حجم وشمعة فوق المقاومة أو ارتداد مؤكد من الدعم",
    })
    if not quality.accepted:
        snapshot["watch_reason"] = quality.reasons[0]
        snapshot["activation_condition"] = "تحديث السعر وBid/Ask وتقلص السبريد ثم إعادة فحص الإشارة"
        return None, quality.reasons, snapshot
    if (indicators.get("average_volume") or 0) < settings.min_avg_daily_volume:
        return None, ["متوسط حجم التداول أقل من الحد المسموح"], snapshot
    if (indicators.get("relative_volume") or 0) < settings.min_relative_volume:
        return None, ["الحجم النسبي أقل من الحد المسموح"], snapshot

    news_provider = get_news_provider(settings)
    try:
        news = news_provider.get_news(symbol)
    except Exception:
        news = []
        quality.warnings.append("تعذر مزود الأخبار؛ لم يتم اختراع أخبار بديلة")
    risk_flags = sorted({flag for item in news for flag in item.risk_flags})
    if risk_flags:
        return None, ["مخاطر جوهرية في الأخبار: " + "، ".join(risk_flags)], snapshot
    for item in news:
        db.add(NewsItem(
            symbol=symbol, headline=item.headline, source=item.source,
            published_at=item.published_at, url=item.url,
            classification=item.classification, is_official=item.is_official,
            risk_flags=item.risk_flags,
        ))

    strategy = select_strategy(
        indicators, quote.price, regime,
        verified_news=any(item.is_official for item in news),
    )
    if strategy.strategy_id == "no_trade":
        return None, [strategy.reason], snapshot
    atr = max(float(indicators.get("atr") or quote.price * 0.03), quote.price * 0.01)
    support = float(indicators.get("support") or quote.bid)
    resistance = float(indicators.get("resistance") or quote.ask)
    entry = max(float(quote.ask), resistance + 0.01) if strategy.strategy_id in {"volume_breakout", "opening_range_breakout"} else max(float(quote.ask), quote.price)
    stop = min(support - 0.01, entry - atr * 0.8)
    stop = max(0.01, round(stop, 2))
    entry = round(entry, 2)
    if entry <= stop:
        return None, ["لا يمكن بناء وقف خسارة منطقي"], snapshot
    risk_per_share = entry - stop
    target1 = round(entry + risk_per_share * settings.min_risk_reward, 2)
    target2 = round(entry + risk_per_share * (settings.min_risk_reward + 1), 2)
    rr = risk_reward(entry, stop, target1)
    if rr < settings.min_risk_reward:
        return None, ["العائد إلى المخاطرة أقل من الحد المطلوب"], snapshot
    valid_minutes = strategy.valid_minutes
    expires = datetime.now(timezone.utc) + timedelta(minutes=valid_minutes)
    technical = min(100, strategy.score + int(min(indicators.get("relative_volume") or 0, 3) * 3))
    liquidity = max(0, min(100, int(100 - float(quote.spread_pct or 100) * 15)))
    news_score = 50 if not news else max(0, 65 - len(risk_flags) * 25)
    overall = round(technical * 0.55 + liquidity * 0.3 + news_score * 0.15)
    risk_settings = _risk_settings(db, settings)
    plan = position_size(
        float(risk_settings.capital_sar), float(risk_settings.max_risk_pct),
        entry, stop, [target1, target2], settings.usd_sar_rate,
    )
    warnings = list(quality.warnings)
    if not news:
        warnings.append("الأخبار غير متوفرة؛ لم تُستخدم أخبار في التقييم")
    result = OpportunityResult(
        symbol=symbol,
        company_name=symbol,
        status=OpportunityStatus.CONDITIONAL_ENTRY,
        strategy_id=strategy.strategy_id,
        strategy_name_ar=strategy.name_ar,
        strategy_name_en=strategy.name_en,
        market_regime=regime,
        session=quote.session,
        current_price=round(quote.price, 4),
        change_pct=round(
            (quote.price / float(daily["close"].astype(float).iloc[-2]) - 1) * 100,
            2,
        ) if len(daily) >= 2 and float(daily["close"].astype(float).iloc[-2]) else 0.0,
        bid=round(float(quote.bid), 4),
        ask=round(float(quote.ask), 4),
        spread_pct=round(float(quote.spread_pct), 2),
        data_source=quote.provider if quote.provider != "unknown" else provider.provider_name,
        data_feed=quote.feed,
        is_delayed=quote.is_delayed,
        quote_timestamp=_quote_time(quote),
        quote_age_seconds=quote.age_seconds,
        entry_zone=EntryZone(**{"from": entry, "to": round(entry + min(atr * 0.2, float(quote.ask) * 0.01), 2)}),
        entry_trigger=strategy.trigger,
        stop_loss=stop,
        stop_reason=f"أسفل مستوى الإبطال الفني؛ {strategy.invalidation}",
        targets=[
            Target(price=target1, label="الهدف الأول", estimated_horizon="خلال الجلسة"),
            Target(price=target2, label="الهدف الثاني", estimated_horizon="خلال يوم إلى يومين"),
        ],
        risk_reward=rr,
        valid_for_minutes=valid_minutes,
        expires_at=expires,
        invalidation_conditions=[strategy.invalidation, "هبوط الحجم", "اتساع السبريد", "صدور خبر سلبي جوهري"],
        technical_score=technical,
        news_score=news_score,
        liquidity_score=liquidity,
        overall_score=overall,
        confidence_label="مرتفعة" if overall >= 80 else "متوسطة" if overall >= 65 else "منخفضة",
        reasons_ar=[strategy.reason, f"الحجم النسبي {indicators.get('relative_volume', 0):.2f}"],
        warnings_ar=warnings[:4],
        news_summary_ar="لا توجد أخبار متاحة من مزود رسمي" if not news else news[0].headline,
        analysis_summary_ar=(
            "المعطيات الفنية متوافقة بصورة مشروطة؛ تبقى القراءة صالحة فقط بعد تحقق "
            "الشرط الفني وضمن مدة الصلاحية."
        ),
        suggested_shares=plan.shares,
        position_value_usd=plan.position_value_usd,
        max_loss_sar=plan.max_loss_sar,
        capital_used_pct=plan.capital_used_pct,
        estimated_profit_sar=plan.estimated_profit_sar,
    )
    snapshot["result"] = result.model_dump(mode="json", by_alias=True)
    return result, [], snapshot


def persist_opportunity(db: Session, result: OpportunityResult, scan_run_id=None) -> StockOpportunity:
    dumped = result.model_dump(mode="json", by_alias=True)
    mark = hashlib.sha256(json.dumps(dumped, sort_keys=True).encode()).hexdigest()
    row = StockOpportunity(
        scan_run_id=scan_run_id, symbol=result.symbol, company_name=result.company_name,
        status=result.status.value, strategy_id=result.strategy_id,
        market_regime=result.market_regime.value, expires_at=result.expires_at,
        quote_timestamp=result.quote_timestamp, price_at_analysis=result.current_price,
        entry_from=result.entry_zone.from_price, entry_to=result.entry_zone.to,
        stop_loss=result.stop_loss, risk_reward=result.risk_reward,
        overall_score=result.overall_score, result_json=dumped, data_fingerprint=mark,
    )
    db.add(row)
    db.flush()
    for index, target in enumerate(result.targets, 1):
        db.add(OpportunityTarget(opportunity_id=row.id, sequence=index, price=target.price, label=target.label))
    db.add(OpportunityEvent(opportunity_id=row.id, event_type="detected", price=result.current_price))
    return row


def scan_market(
    db: Session,
    provider: MarketDataAdapter,
    settings: Settings,
    run,
    min_price: float | None = None,
    max_price: float | None = None,
    universe_limit: int | None = None,
) -> list[OpportunityResult]:
    regime, regime_inputs = classify_market(provider)
    db.add(MarketRegimeRecord(regime=regime.value, session=current_session(), inputs_json=regime_inputs))
    limit = max(1, min(int(universe_limit or settings.scan_universe_limit), 5000))
    symbols = provider.list_active_us_symbols(limit) or settings.configured_scan_symbols
    symbols = list(dict.fromkeys(symbols))[:limit]
    run.symbols_total = len(symbols)
    run.provider = provider.provider_name
    db.commit()
    quote_map = provider.get_quotes_many(symbols) if provider.supports_batch_quotes else {}
    run.progress_pct = 12
    db.commit()
    daily_map = provider.get_daily_ohlcv_many(symbols, 30) if provider.supports_batch_daily_ohlcv else {}
    run.progress_pct = 28
    run.symbols_scanned = len(quote_map)
    db.commit()
    eligible_symbols: list[str] = []
    staged: dict[str, tuple[str, list[str], dict]] = {}
    for symbol in symbols:
        quote = quote_map.get(symbol)
        daily_frame = daily_map.get(symbol)
        if quote is None or daily_frame is None or daily_frame.empty:
            staged[symbol] = (
                "failed", ["تعذر جلب البيانات"],
                {"stage": "failed", "failure_category": "تعذر جلب البيانات"},
            )
            continue
        if min_price is not None and quote.price < min_price:
            staged[symbol] = (
                "skipped", ["السعر خارج الفلتر الاختياري"],
                {"stage": "skipped", "price": quote.price, "skip_category": "فلتر السعر"},
            )
            continue
        if max_price is not None and quote.price > max_price:
            staged[symbol] = (
                "skipped", ["السعر خارج الفلتر الاختياري"],
                {"stage": "skipped", "price": quote.price, "skip_category": "فلتر السعر"},
            )
            continue
        eligible_symbols.append(symbol)

    # The provider can batch-screen the whole universe, but expensive intraday
    # analysis is limited to the most liquid coarse candidates.
    def coarse_liquidity(symbol: str) -> float:
        frame = daily_map.get(symbol)
        if frame is None or frame.empty:
            return 0.0
        return float(frame["close"].iloc[-1]) * float(frame["volume"].tail(20).mean())

    eligible_symbols.sort(key=coarse_liquidity, reverse=True)
    deep_limit = max(1, min(settings.scan_detailed_limit, 20))
    deep_symbols = eligible_symbols[:deep_limit]
    deep_daily_map = (
        provider.get_daily_ohlcv_many(deep_symbols, 220)
        if provider.supports_batch_daily_ohlcv and deep_symbols else {}
    )
    for symbol in eligible_symbols[deep_limit:]:
        quote = quote_map[symbol]
        frame = daily_map[symbol]
        staged[symbol] = (
            "skipped", ["خارج أفضل المرشحين بعد الترتيب الرقمي"],
            {
                "stage": "skipped", "skip_category": "الترتيب الرقمي",
                "price": quote.price,
                "liquidity": coarse_liquidity(symbol),
                "change_pct": round(
                    (quote.price / float(frame["close"].iloc[-2]) - 1) * 100, 2
                ) if len(frame) >= 2 else 0,
            },
        )

    for symbol, (_, reasons, snapshot) in staged.items():
        db.add(StockCandidate(
            scan_run_id=run.id, symbol=symbol, accepted=False,
            numeric_score=0, exclusion_reasons=reasons, snapshot_json=snapshot,
        ))

    accepted: list[OpportunityResult] = []
    for index, symbol in enumerate(deep_symbols, 1):
        try:
            result, reasons, snapshot = build_opportunity(
                db, provider, settings, symbol, regime, run.id,
                quote_override=quote_map.get(symbol),
                daily_override=deep_daily_map.get(symbol),
            )
            observed_price = snapshot.get("price")
            if observed_price is not None and (
                (min_price is not None and observed_price < min_price)
                or (max_price is not None and observed_price > max_price)
            ):
                result = None
                reasons = ["السعر خارج نطاق الماسح المحدد"]
        except Exception as exc:
            result, reasons, snapshot = None, ["تعذر تحليل بيانات السهم"], {"error_type": type(exc).__name__}
        score = result.overall_score if result else min(
            59,
            round(
                min(float(snapshot.get("indicators", {}).get("relative_volume") or 0), 3) * 12
                + (20 if snapshot.get("trend") == "صاعد" else 8)
                + (15 if snapshot.get("support") and snapshot.get("resistance") else 0)
            ),
        )
        snapshot["stage"] = "candidate" if result else "analyzed"
        snapshot["watch_reason"] = reasons[0] if reasons else snapshot.get("watch_reason")
        db.add(StockCandidate(
            scan_run_id=run.id, symbol=symbol, accepted=result is not None,
            numeric_score=score, exclusion_reasons=reasons, snapshot_json=snapshot,
        ))
        if result:
            accepted.append(result)
        run.symbols_scanned = len(quote_map)
        run.symbols_excluded = len(staged) + index - len(accepted)
        run.progress_pct = 35 + int(index / max(len(deep_symbols), 1) * 45)
        if index % 10 == 0:
            db.commit()
    accepted.sort(key=lambda item: item.overall_score, reverse=True)
    shortlist = accepted[: min(settings.openai_candidate_limit, 5)]
    reviews = review_candidates(
        db, settings,
        [
            {
                "symbol": item.symbol, "strategy_id": item.strategy_id,
                "market_regime": item.market_regime.value,
                "scores": {
                    "technical": item.technical_score, "news": item.news_score,
                    "liquidity": item.liquidity_score, "overall": item.overall_score,
                },
                "quote": {
                    "price": item.current_price, "bid": item.bid, "ask": item.ask,
                    "spread_pct": item.spread_pct, "age_seconds": item.quote_age_seconds,
                },
                "entry": item.entry_zone.model_dump(by_alias=True),
                "stop": item.stop_loss,
                "targets": [target.price for target in item.targets],
                "warnings": item.warnings_ar,
            }
            for item in shortlist
        ],
    )
    final: list[OpportunityResult] = []
    for item in shortlist:
        review = reviews.get(item.symbol)
        if review and not review.approved:
            continue
        if review:
            item.reasons_ar = review.reasons_ar or item.reasons_ar
            item.warnings_ar = review.warnings_ar or item.warnings_ar
            item.analysis_summary_ar = review.analysis_summary_ar
            item.confidence_label = review.confidence_label
        persist_opportunity(db, item, run.id)
        final.append(item)
        if len(final) >= settings.max_results:
            break
    db.commit()
    return final


def expire_old_opportunities(db: Session) -> int:
    rows = db.scalars(
        select(StockOpportunity).where(
            StockOpportunity.expires_at <= datetime.now(timezone.utc),
            StockOpportunity.status != OpportunityStatus.EXPIRED.value,
        )
    ).all()
    for row in rows:
        row.status = OpportunityStatus.EXPIRED.value
        row.result_json = {**row.result_json, "status": OpportunityStatus.EXPIRED.value}
        db.add(OpportunityEvent(opportunity_id=row.id, event_type="expired", price=None))
    db.commit()
    return len(rows)
