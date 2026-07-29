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
) -> tuple[OpportunityResult | None, list[str], dict]:
    quote = provider.get_quote(symbol)
    quality = evaluate_quote(quote, settings)
    snapshot: dict = {
        "symbol": symbol,
        "price": quote.price,
        "bid": quote.bid,
        "ask": quote.ask,
        "spread_pct": quote.spread_pct,
        "quote_age_seconds": quote.age_seconds,
    }
    if not quality.accepted:
        return None, quality.reasons, snapshot
    daily = provider.get_daily_ohlcv(symbol, 220)
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

    strategy = select_strategy(indicators, quote.price, regime)
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
        current_price=round(quote.price, 4),
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
        analysis_summary_ar="فرصة مشروطة؛ لا تُفعّل إلا بعد تحقق شرط الدخول ضمن مدة الصلاحية.",
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


def scan_market(db: Session, provider: MarketDataAdapter, settings: Settings, run) -> list[OpportunityResult]:
    regime, regime_inputs = classify_market(provider)
    db.add(MarketRegimeRecord(regime=regime.value, session=current_session(), inputs_json=regime_inputs))
    symbols = provider.list_active_us_symbols(settings.scan_universe_limit) or settings.configured_scan_symbols
    symbols = list(dict.fromkeys(symbols))[: settings.scan_universe_limit]
    run.symbols_total = len(symbols)
    run.provider = provider.provider_name
    db.commit()
    accepted: list[OpportunityResult] = []
    for index, symbol in enumerate(symbols, 1):
        try:
            result, reasons, snapshot = build_opportunity(db, provider, settings, symbol, regime, run.id)
        except Exception as exc:
            result, reasons, snapshot = None, ["تعذر تحليل بيانات السهم"], {"error_type": type(exc).__name__}
        score = result.overall_score if result else 0
        db.add(StockCandidate(
            scan_run_id=run.id, symbol=symbol, accepted=result is not None,
            numeric_score=score, exclusion_reasons=reasons, snapshot_json=snapshot,
        ))
        if result:
            accepted.append(result)
        run.symbols_scanned = index
        run.symbols_excluded = index - len(accepted)
        run.progress_pct = int(index / max(len(symbols), 1) * 80)
        if index % 10 == 0:
            db.commit()
    accepted.sort(key=lambda item: item.overall_score, reverse=True)
    shortlist = accepted[: settings.openai_candidate_limit]
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
