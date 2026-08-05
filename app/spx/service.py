from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import repository
from app.db.models import SPXHuntResult, SPXSyntheticObservation
from app.news.service import UnifiedNewsService
from app.options.market_clock import (
    NEW_YORK,
    RIYADH,
    market_session,
    serialize_market_session,
    spx_options_session,
)
from app.spx.engine import (
    breakout_outlook,
    directional_scenario,
    escape_reason,
    rank_contracts,
    technical_analysis,
)
from app.spx.providers import AlpacaSPXProvider
from app.spx.review import review_spx
from app.spx.schemas import (
    Direction,
    SPXHunterResult,
    SPXProviderCapabilities,
    SPXSyntheticValue,
    StrikeMode,
)
from app.spx.synthetic import SOURCE, calculate_synthetic_value

CACHE_PREFIX = "spx:hunter"


def build_synthetic_review_payload(
    synthetic: SPXSyntheticValue,
    technical: dict,
    scenario: dict | None,
    news: list[dict],
    contracts: list,
) -> dict:
    """Build bounded OPRA context; TradingView is display-only."""
    return {
        "symbol": "Synthetic SPX",
        "review_scope": "direction_and_contracts" if contracts else "direction_only",
        "data_label": "estimated_synthetic_forward_not_official_spx",
        "source": synthetic.source,
        "data_version": synthetic.calculation_timestamp.isoformat(),
        "synthetic_quality": {
            "provider_status": synthetic.provider_status,
            "pairs_used": synthetic.pairs_used,
            "confidence_score": synthetic.confidence_score,
            "data_quality_score": synthetic.data_quality_score,
            "liquidity_score": synthetic.liquidity_score,
            "median_quote_age_seconds": synthetic.median_quote_age_seconds,
            "expiration_used": synthetic.expiration_used,
            "settlement_type": synthetic.settlement_type,
        },
        "technical_direction": {
            key: technical.get(key)
            for key in (
                "price", "session_open", "session_change_points",
                "session_change_pct", "sample_size", "sample_span_minutes",
                "trend_ready", "direction", "direction_clarity_score",
                "momentum_score", "support", "resistance", "expected_move",
                "entry_condition",
            )
        },
        # The deterministic summary above contains the direction, momentum,
        # levels and sample quality. Raw price history is intentionally kept
        # local and is never sent to an LLM.
        "scenario": scenario,
        "trusted_news": news[:5],
        "contracts": [
            item.model_dump(mode="json") if hasattr(item, "model_dump") else item
            for item in contracts[:3]
        ],
        "allowed_decisions": [
            "الاتجاه الصاعد مدعوم", "الاتجاه الهابط مدعوم",
            "الاتجاه غير محسوم", "انتظر", "لا صفقة",
        ],
    }


def _unavailable_capability(message: str) -> SPXProviderCapabilities:
    return SPXProviderCapabilities(
        provider="alpaca",
        checked_at=datetime.now(timezone.utc),
        message_ar=message,
        underlying_status="unavailable",
        options_status="unknown",
    )


def spx_min_dte(settings: Settings) -> int:
    """Apply the documented SPX expiry flags without silently skipping 1DTE."""
    if settings.spx_allow_0dte:
        return 0
    if settings.spx_allow_1dte:
        return 1
    return 2


class SPXHunterService:
    def __init__(self, db: Session, settings: Settings, provider=None):
        self.db = db
        self.settings = settings
        self.provider = provider

    def _provider(self):
        return self.provider or AlpacaSPXProvider(self.settings)
    def _news(self) -> list[dict]:
        if not self.settings.spx_news_enabled:
            return []
        try:
            return UnifiedNewsService(self.db, self.settings).spx_context().get("items", [])[:5]
        except Exception:
            return []

    def _preserved_ai_review(
        self, mode: StrikeMode, technical: dict
    ) -> dict | None:
        """Keep a recent review across data-only background refreshes."""
        cached = repository.cache_get_any(
            self.db, f"{CACHE_PREFIX}:{mode.value}"
        ) or {}
        review = cached.get("ai_review") or {}
        if review.get("status") != "completed" or not review.get("reviewed_at"):
            return None
        if review.get("reviewed_direction") != technical.get("direction"):
            return None
        try:
            reviewed_at = datetime.fromisoformat(
                str(review["reviewed_at"]).replace("Z", "+00:00")
            )
            if reviewed_at.tzinfo is None:
                reviewed_at = reviewed_at.replace(tzinfo=timezone.utc)
            age = (
                datetime.now(timezone.utc) - reviewed_at.astimezone(timezone.utc)
            ).total_seconds()
            return (
                review
                if age <= self.settings.spx_ai_review_max_age_seconds
                else None
            )
        except Exception:
            return None

    def refresh(
        self,
        mode: StrikeMode | str | None = None,
        *,
        allow_ai_review: bool = True,
    ) -> dict:
        mode = StrikeMode(mode or self.settings.spx_default_strike_mode)
        now = datetime.now(timezone.utc)
        session = market_session(now)
        market = serialize_market_session(session)
        market.update({
            "source": "Alpaca",
            "opra_status": "unknown",
            "last_quote": None,
            "last_trade": None,
            "data_age_seconds": None,
            "realtime": False,
            "contracts_actionable": False,
            "monitoring_only": True,
            "data_state": "NO_DATA",
        })
        if not self.settings.spx_enabled:
            result = SPXHunterResult(
                generated_at=now, status="disabled", decision="no_trade",
                decision_ar="لا صفقة", reason_ar="قنّاص SPX غير مفعل.",
                strike_mode=mode, capabilities=_unavailable_capability("قنّاص SPX غير مفعل"),
                market=market, warnings_ar=["Paper Trading ومراقبة فقط."],
            )
            return self._save(result)
        try:
            provider = self._provider()
            capabilities = provider.capabilities()
        except Exception as exc:
            result = SPXHunterResult(
                generated_at=now, status="provider_failed", decision="escape",
                decision_ar="اهرب الآن", reason_ar="تعذر التحقق من مزود SPX.",
                strike_mode=mode,
                capabilities=_unavailable_capability(f"تعذر المزود ({type(exc).__name__})"),
                market=market, refresh_required=True,
                warnings_ar=["فشل المزود لا يعطل بقية المنصة.", "Paper Trading ومراقبة فقط."],
            )
            return self._save(result)
        market["opra_status"] = "available" if capabilities.opra_available else "unavailable"
        if (
            self.settings.spx_underlying_provider == "synthetic_opra"
            and self.settings.spx_synthetic_enabled
        ):
            return self._refresh_synthetic(
                provider=provider,
                capabilities=capabilities,
                market=market,
                session=session,
                mode=mode,
                now=now,
                allow_ai_review=allow_ai_review,
            )
        if not capabilities.underlying_available:
            result = SPXHunterResult(
                generated_at=now, status="underlying_unavailable", decision="escape",
                decision_ar="اهرب الآن", reason_ar="بيانات SPX غير متاحة من المزود الحالي",
                strike_mode=mode, capabilities=capabilities, market=market,
                refresh_required=True,
                warnings_ar=[
                    "لن تُستخدم بيانات SPY أو XSP بدل SPX.",
                    "سلسلة العقود وحدها لا تكفي لبناء قنصة آمنة.",
                    "Paper Trading ومراقبة فقط.",
                ],
            )
            return self._save(result)
        try:
            quote = provider.get_quote()
            history = provider.get_history()
            age = max(0, int((now - quote.quote_timestamp.astimezone(timezone.utc)).total_seconds()))
            market.update({
                "last_quote": quote.quote_timestamp.isoformat(),
                "last_trade": quote.trade_timestamp.isoformat() if quote.trade_timestamp else None,
                "data_age_seconds": age,
                "realtime": quote.is_realtime,
            })
            if age > self.settings.spx_max_data_age_seconds:
                raise StaleSPXData
            market["data_state"] = "LIVE"
            technical = technical_analysis(history, quote)
        except StaleSPXData:
            market["data_state"] = "STALE"
            result = SPXHunterResult(
                generated_at=now, status="stale", decision="escape",
                decision_ar="اهرب الآن", reason_ar="البيانات قديمة",
                strike_mode=mode, capabilities=capabilities, market=market,
                refresh_required=True, warnings_ar=["لا صفقة عند تقادم بيانات SPX."],
            )
            return self._save(result)
        except Exception as exc:
            result = SPXHunterResult(
                generated_at=now, status="underlying_failed", decision="escape",
                decision_ar="اهرب الآن", reason_ar="تعذر تكوين التحليل الفني لـSPX.",
                strike_mode=mode, capabilities=capabilities, market=market,
                refresh_required=True, warnings_ar=[f"مزود SPX: {type(exc).__name__}", "لم تُجلب سلسلة العقود."],
            )
            return self._save(result)
        news = self._news()
        news_impact_score = max(
            [int(item.get("spx_impact_score", 0)) for item in news] or [0]
        )
        technical["breakout_outlook"] = breakout_outlook(
            technical, news_impact_score=news_impact_score
        )
        direction, scenario, scenario_decision = directional_scenario(technical, news)
        if direction == Direction.NONE or scenario is None:
            result = SPXHunterResult(
                generated_at=now, status="no_trade", decision="wait",
                decision_ar="انتظر", reason_ar=scenario_decision,
                strike_mode=mode, capabilities=capabilities, market=market,
                quote=quote.model_dump(mode="json"), technical=technical,
                news=news, news_impact_score=max([int(item.get("spx_impact_score", 0)) for item in news] or [0]),
                warnings_ar=["اهرب الآن — لا توجد فرصة مستوفية للشروط."],
            )
            return self._save(result)
        try:
            contracts = provider.get_chain(
                min_dte=spx_min_dte(self.settings),
                max_dte=21,
                underlying_price=quote.price,
            )
            ranked, rejected = rank_contracts(
                contracts, direction=direction, scenario=scenario, underlying=quote.price,
                mode=mode, settings=self.settings, session=session, now=now,
            )
        except Exception as exc:
            ranked, rejected = [], {f"provider_{type(exc).__name__}": 1}
        session_ok = spx_options_session(
            now, allow_global=self.settings.spx_global_trading_hours
        )
        ranked = [item.model_copy(update={"actionable": session_ok}) for item in ranked]
        best = ranked[0] if ranked and session_ok else None
        escape = escape_reason(
            technical=technical, session=session, data_age=market["data_age_seconds"],
            news=news, best=best, settings=self.settings,
            options_session_open=session_ok,
        )
        market["contracts_actionable"] = bool(best and best.actionable)
        if escape:
            decision, decision_ar, reason = "escape", "اهرب الآن", escape
        elif not best:
            decision, decision_ar, reason = "no_trade", "لا صفقة", "لا يوجد عقد مستوفٍ للفلاتر."
        else:
            decision, decision_ar, reason = "conditional_hunt", "قنص مشروط", (
                f"{direction.value.upper()} قريب من السترايك بعد تحقق شرط SPX وإعادة تسعير Bid/Ask."
            )
        ai = None
        if best and allow_ai_review:
            ai = review_spx(self.db, self.settings, {
                "symbol": "SPX", "scenario": scenario, "trusted_news": news[:5],
                "contracts": [item.model_dump(mode="json") for item in ranked[:3]],
                "allowed_decisions": ["قنص مشروط", "انتظر", "اهرب الآن", "لا صفقة"],
            })
            if ai.get("status") == "completed" and not ai.get("approved", True):
                decision, decision_ar, reason = "no_trade", "لا صفقة", ai.get("explanation_ar", "رفض المراجع القنصة الضعيفة.")
                best = None
        result = SPXHunterResult(
            generated_at=now, status="ready" if best else "no_trade",
            decision=decision, decision_ar=decision_ar, reason_ar=reason,
            strike_mode=mode, capabilities=capabilities, market=market,
            quote=quote.model_dump(mode="json"), technical=technical, news=news,
            news_impact_score=max([int(item.get("spx_impact_score", 0)) for item in news] or [0]),
            direction=direction, scenario=scenario, best_contract=best,
            ranked_contracts=ranked, rejected_contracts=rejected, ai_review=ai,
            refresh_required=not session_ok,
            warnings_ar=[
                "Paper Trading ومراقبة فقط — لا توجد أوامر حقيقية.",
                "دخول مشروط، وليس سعرًا مضمونًا.",
                "هذه درجات تقديرية للمقارنة مبنية على البيانات الحالية، وليست ضمانًا.",
                "سعر العقد تقديري وقد يختلف بسبب IV وTheta والسبريد وسرعة الحركة.",
                *(
                    ["0DTE عالي الخطورة وقد يفقد معظم قيمته خلال دقائق. Paper Trading فقط وعقد واحد كحد أقصى."]
                    if self.settings.spx_allow_0dte else []
                ),
                *([] if mode == StrikeMode.NEAR else ["العقد الأبعد أرخص، لكنه يحتاج حركة أسرع وأقوى."]),
                *([] if session_ok else ["قراءة للمراقبة فقط — أعد تسعير العقد بعد افتتاح جلسة SPX."]),
            ],
        )
        return self._save(result)

    def _synthetic_failure(
        self,
        *,
        mode: StrikeMode,
        capabilities: SPXProviderCapabilities,
        market: dict,
        synthetic: SPXSyntheticValue,
        now: datetime,
    ) -> dict:
        decision = (
            "market_closed"
            if synthetic.provider_status == "options_closed"
            else "insufficient_data"
        )
        decision_ar = (
            "سوق الخيارات مغلق"
            if synthetic.provider_status == "options_closed"
            else "بيانات غير كافية"
        )
        result = SPXHunterResult(
            generated_at=now,
            status=synthetic.provider_status,
            decision=decision,
            decision_ar=decision_ar,
            reason_ar=synthetic.status_message_ar,
            strike_mode=mode,
            capabilities=capabilities,
            market=market,
            synthetic=synthetic,
            refresh_required=True,
            warnings_ar=[
                "هذه قيمة ضمنية محسوبة من عقود Call وPut عبر OPRA، وليست قيمة SPX الرسمية المنشورة.",
                "Paper Trading ومراقبة فقط — لا توجد أوامر حقيقية.",
            ],
        )
        return self._save(result)

    @staticmethod
    def _next_spx_open_ar(session) -> str:
        """When SPX options next trade, in Riyadh time.

        SPX opens on Cboe's global session at 20:15 New York, hours before the
        regular bell the rest of the platform counts down to, so reusing the
        equity figure would send the reader away for nothing.
        """
        regular_open = session.next_options_open_at
        global_open = regular_open.replace(hour=20, minute=15) - timedelta(days=1)
        opens_at = global_open if global_open > session.new_york_time else regular_open
        riyadh = opens_at.astimezone(RIYADH)
        return riyadh.strftime("%A %H:%M بتوقيت الرياض")

    def _latest_synthetic(self, now: datetime) -> SPXSyntheticValue | None:
        row = self.db.scalar(
            select(SPXSyntheticObservation)
            .order_by(SPXSyntheticObservation.observed_at.desc())
            .limit(1)
        )
        if row is None:
            return None
        try:
            payload = dict(row.payload_json or {})
            payload.update(
                provider_status="options_closed",
                status_message_ar="سوق الخيارات مغلق — آخر قراءة للمراقبة فقط",
            )
            return SPXSyntheticValue.model_validate(payload)
        except Exception:
            return None

    def _synthetic_technical(
        self, synthetic: SPXSyntheticValue
    ) -> dict:
        observed_at = synthetic.calculation_timestamp
        if observed_at.tzinfo is None:
            observed_at = observed_at.replace(tzinfo=timezone.utc)
        eastern = observed_at.astimezone(NEW_YORK)
        session_start = eastern.replace(
            hour=0, minute=0, second=0, microsecond=0
        ).astimezone(timezone.utc)
        rows = list(
            self.db.scalars(
                select(SPXSyntheticObservation)
                .where(
                    SPXSyntheticObservation.observed_at >= session_start,
                    SPXSyntheticObservation.expiration == synthetic.expiration_used,
                    SPXSyntheticObservation.settlement_type == synthetic.settlement_type,
                    SPXSyntheticObservation.source == synthetic.source,
                )
                .order_by(SPXSyntheticObservation.observed_at.desc())
                .limit(60)
            )
        )
        observations = [
            {
                "time": (
                    row.observed_at.replace(tzinfo=timezone.utc)
                    if row.observed_at.tzinfo is None
                    else row.observed_at.astimezone(timezone.utc)
                ),
                "value": float(row.forward_value),
            }
            for row in reversed(rows)
            if row.forward_value is not None
        ]
        current = float(synthetic.synthetic_forward_value or 0)
        if (
            not observations
            or abs(observations[-1]["value"] - current) > 1e-9
            or abs((observed_at - observations[-1]["time"]).total_seconds()) >= 1
        ):
            observations.append({"time": observed_at, "value": current})
        values = [item["value"] for item in observations]
        support = min(values[-20:])
        resistance = max(values[-20:])
        changes = [
            abs(values[index] - values[index - 1])
            for index in range(1, len(values))
        ]
        expected_move = max(
            0.5,
            sum(changes[-10:]) / max(1, len(changes[-10:])),
        )
        if len(values) < 6:
            direction = Direction.NONE
            clarity = 0
            momentum_score = 0
        else:
            fast = sum(values[-3:]) / 3
            slow = sum(values[-6:]) / 6
            momentum = values[-1] - values[-4]
            threshold = max(0.25, expected_move * 0.35)
            if fast > slow and momentum > threshold:
                direction = Direction.CALL
            elif fast < slow and momentum < -threshold:
                direction = Direction.PUT
            else:
                direction = Direction.NONE
            clarity = min(
                88,
                round(
                    55
                    + abs(fast - slow) / max(0.1, expected_move) * 18
                    + abs(momentum) / max(0.1, expected_move) * 8
                ),
            ) if direction != Direction.NONE else 35
            momentum_score = min(
                90,
                round(abs(momentum) / max(0.1, expected_move) * 25),
            )
        buffer = max(0.25, expected_move * 0.2)
        if direction == Direction.CALL:
            entry = current + buffer
            stop = current - max(1.0, expected_move * 1.5)
            targets = [
                entry + expected_move * factor for factor in (1.0, 1.7, 2.5)
            ]
            trigger = (
                f"اختراق قيمة SPX الضمنية {entry:.2f} والثبات فوقها "
                "مع استمرار الزخم وحداثة OPRA"
            )
        elif direction == Direction.PUT:
            entry = current - buffer
            stop = current + max(1.0, expected_move * 1.5)
            targets = [
                entry - expected_move * factor for factor in (1.0, 1.7, 2.5)
            ]
            trigger = (
                f"كسر قيمة SPX الضمنية {entry:.2f} والثبات تحتها "
                "مع استمرار الزخم وحداثة OPRA"
            )
        else:
            entry, stop, targets = None, None, []
            trigger = "انتظر اكتمال سلسلة Synthetic SPX وتوافق الزخم."
        risk = abs((entry or current) - (stop or current))
        reward = abs((targets[1] if len(targets) > 1 else current) - (entry or current))
        return {
            "price": round(current, 2),
            "source": SOURCE,
            "is_official_spx": False,
            "sample_size": len(values),
            "trend_ready": len(values) >= 6,
            "samples_required": 6,
            "session_open": round(values[0], 2),
            "session_change_points": round(current - values[0], 2),
            "session_change_pct": round(
                (current / values[0] - 1) * 100, 3
            ) if values[0] else 0,
            "sample_span_minutes": round(
                (observations[-1]["time"] - observations[0]["time"]).total_seconds()
                / 60,
                1,
            ),
            "series": [
                {"time": item["time"].isoformat(), "value": round(item["value"], 2)}
                for item in observations[-60:]
            ],
            "support": round(support, 2),
            "resistance": round(resistance, 2),
            "expected_move": round(expected_move, 2),
            "direction": direction.value,
            "direction_clarity_score": clarity,
            "momentum_score": momentum_score,
            "timeframe_alignment_score": clarity,
            "entry_condition": trigger,
            "entry": round(entry, 2) if entry is not None else None,
            "invalidation": round(stop, 2) if stop is not None else None,
            "stop": round(stop, 2) if stop is not None else None,
            "targets": [round(value, 2) for value in targets],
            "risk_reward": round(reward / risk, 2) if risk else 0,
            "valid_minutes": 2,
        }

    def _store_synthetic(self, synthetic: SPXSyntheticValue) -> None:
        if (
            synthetic.provider_status != "ready"
            or synthetic.synthetic_forward_value is None
            or synthetic.lower_bound is None
            or synthetic.upper_bound is None
            or not synthetic.expiration_used
            or not synthetic.settlement_type
        ):
            return
        latest = self.db.scalar(
            select(SPXSyntheticObservation)
            .where(
                SPXSyntheticObservation.expiration == synthetic.expiration_used,
                SPXSyntheticObservation.settlement_type == synthetic.settlement_type,
                SPXSyntheticObservation.source == synthetic.source,
            )
            .order_by(SPXSyntheticObservation.observed_at.desc())
            .limit(1)
        )
        if latest is not None:
            latest_at = latest.observed_at
            if latest_at.tzinfo is None:
                latest_at = latest_at.replace(tzinfo=timezone.utc)
            current_at = synthetic.calculation_timestamp
            if current_at.tzinfo is None:
                current_at = current_at.replace(tzinfo=timezone.utc)
            if (
                current_at.astimezone(timezone.utc)
                - latest_at.astimezone(timezone.utc)
            ).total_seconds() < self.settings.spx_observation_min_spacing_seconds:
                return
        self.db.add(
            SPXSyntheticObservation(
                observed_at=synthetic.calculation_timestamp,
                forward_value=synthetic.synthetic_forward_value,
                spot_estimate=synthetic.synthetic_spot_estimate,
                lower_bound=synthetic.lower_bound,
                upper_bound=synthetic.upper_bound,
                pairs_used=synthetic.pairs_used,
                confidence_score=synthetic.confidence_score,
                data_quality_score=synthetic.data_quality_score,
                expiration=synthetic.expiration_used,
                settlement_type=synthetic.settlement_type,
                source=synthetic.source,
                payload_json=synthetic.model_dump(mode="json"),
            )
        )

    def _refresh_synthetic(
        self,
        *,
        provider,
        capabilities: SPXProviderCapabilities,
        market: dict,
        session,
        mode: StrikeMode,
        now: datetime,
        allow_ai_review: bool,
    ) -> dict:
        market.update(
            source=SOURCE,
            realtime=False,
            contracts_actionable=False,
            monitoring_only=True,
        )
        if not self.settings.options_enabled:
            synthetic = SPXSyntheticValue(
                calculation_timestamp=now,
                provider_status="unavailable",
                status_message_ar="وظائف الخيارات غير مفعلة.",
            )
            return self._synthetic_failure(
                mode=mode,
                capabilities=capabilities,
                market=market,
                synthetic=synthetic,
                now=now,
            )
        # SPX also trades Cboe's global session, so "not the regular session"
        # is not the same as closed. Ask the provider and let the freshness of
        # what comes back decide, rather than refusing before the question.
        spx_open = spx_options_session(
            now, allow_global=self.settings.spx_global_trading_hours
        )
        if not spx_open:
            # Only promise a saved reading when one survived. Offering "the
            # last reading" beside an empty panel reads as a broken sniper,
            # which is how a wiped database looks from the outside.
            synthetic = self._latest_synthetic(now) or SPXSyntheticValue(
                calculation_timestamp=now,
                provider_status="options_closed",
                status_message_ar=(
                    "سوق خيارات SPX مغلق ولا توجد قراءة محفوظة من الجلسة السابقة. "
                    f"الجلسة القادمة تبدأ {self._next_spx_open_ar(session)}."
                ),
            )
            market["data_age_seconds"] = (
                max(
                    0,
                    int(
                        (
                            now
                            - synthetic.calculation_timestamp.astimezone(timezone.utc)
                        ).total_seconds()
                    ),
                )
                if synthetic.synthetic_forward_value is not None
                else None
            )
            return self._synthetic_failure(
                mode=mode,
                capabilities=capabilities,
                market=market,
                synthetic=synthetic,
                now=now,
            )
        if not capabilities.opra_available or not capabilities.option_chain_available:
            synthetic = SPXSyntheticValue(
                calculation_timestamp=now,
                provider_status="opra_unavailable",
                status_message_ar="بيانات OPRA غير متاحة",
            )
            return self._synthetic_failure(
                mode=mode,
                capabilities=capabilities,
                market=market,
                synthetic=synthetic,
                now=now,
            )
        try:
            contracts = provider.get_synthetic_chain(
                min_dte=spx_min_dte(self.settings),
                max_dte=21,
            )
            synthetic = calculate_synthetic_value(
                contracts, self.settings, session, now=now
            )
            # Carry the fetch counts through, so an empty chain says where it
            # emptied instead of only that it did.
            chain_diagnostics = getattr(provider, "last_chain_diagnostics", {}) or {}
            if chain_diagnostics and synthetic.provider_status != "ready":
                synthetic = synthetic.model_copy(
                    update={
                        "rejection_reasons": {
                            **(synthetic.rejection_reasons or {}),
                            **{f"chain_{k}": v for k, v in chain_diagnostics.items()
                               if isinstance(v, int)},
                        }
                    }
                )
        except Exception as exc:
            synthetic = SPXSyntheticValue(
                calculation_timestamp=now,
                provider_status="unavailable",
                status_message_ar="تعذر حساب قيمة SPX الضمنية بجودة كافية.",
                rejection_reasons={f"provider_{type(exc).__name__}": 1},
            )
            contracts = []
        market.update(
            last_quote=synthetic.calculation_timestamp.isoformat(),
            data_age_seconds=synthetic.median_quote_age_seconds,
            realtime=synthetic.provider_status == "ready",
            data_state=(
                "LIVE" if synthetic.provider_status == "ready"
                else "STALE" if synthetic.provider_status == "stale"
                else "NO_DATA" if synthetic.provider_status in {
                    "unavailable", "opra_unavailable"
                }
                else "BLOCKED"
            ),
        )
        if synthetic.provider_status != "ready":
            return self._synthetic_failure(
                mode=mode,
                capabilities=capabilities,
                market=market,
                synthetic=synthetic,
                now=now,
            )
        technical = self._synthetic_technical(synthetic)
        self._store_synthetic(synthetic)
        news = self._news()
        news_impact_score = max(
            [int(item.get("spx_impact_score", 0)) for item in news] or [0]
        )
        technical["breakout_outlook"] = breakout_outlook(
            technical,
            news_impact_score=news_impact_score,
            data_quality_score=synthetic.data_quality_score or 0,
        )
        direction, scenario, scenario_decision = directional_scenario(
            technical, news
        )
        if direction == Direction.NONE or scenario is None:
            ai = self._preserved_ai_review(mode, technical)
            if allow_ai_review and technical.get("trend_ready"):
                ai = review_spx(
                    self.db,
                    self.settings,
                    build_synthetic_review_payload(
                        synthetic, technical, None, news, []
                    ),
                )
            result = SPXHunterResult(
                generated_at=now,
                status="no_trade",
                decision="wait",
                decision_ar="انتظر",
                reason_ar=scenario_decision,
                strike_mode=mode,
                capabilities=capabilities,
                market=market,
                synthetic=synthetic,
                technical=technical,
                news=news,
                news_impact_score=max(
                    [
                        int(item.get("spx_impact_score", 0))
                        for item in news
                    ]
                    or [0]
                ),
                ai_review=ai,
                warnings_ar=[
                    "هذه قيمة ضمنية محسوبة من عقود Call وPut عبر OPRA، وليست قيمة SPX الرسمية المنشورة.",
                    "لا صفقة قبل اكتمال اتجاه Synthetic SPX وتوافق شروط الجودة.",
                    "Paper Trading ومراقبة فقط.",
                ],
            )
            return self._save(result)
        ranked, rejected = rank_contracts(
            contracts,
            direction=direction,
            scenario=scenario,
            underlying=float(synthetic.synthetic_forward_value),
            mode=mode,
            settings=self.settings,
            session=session,
            now=now,
        )
        synthetic_ok = bool(
            synthetic.provider_status == "ready"
            and synthetic.synthetic_forward_value is not None
            and synthetic.expiration_used
            and synthetic.settlement_type
            and synthetic.pairs_used >= self.settings.spx_synthetic_min_pairs
            and synthetic.data_quality_score >= self.settings.spx_synthetic_min_data_quality_score
            and synthetic.confidence_score >= self.settings.spx_synthetic_min_confidence_score
            and synthetic.median_quote_age_seconds is not None
            and synthetic.median_quote_age_seconds <= self.settings.spx_synthetic_max_quote_age_seconds
        )
        session_ok = spx_options_session(
            now, allow_global=self.settings.spx_global_trading_hours
        )
        ranked = [item.model_copy(update={"actionable": synthetic_ok and session_ok}) for item in ranked]
        best = ranked[0] if ranked and synthetic_ok and session_ok else None
        market["contracts_actionable"] = bool(best and best.actionable)
        market["monitoring_only"] = not bool(best)
        decision = "conditional_hunt" if best else "no_trade"
        decision_ar = "قنص مشروط" if best else "لا صفقة"
        reason_ar = (
            f"{direction.value.upper()} بعد تحقق شرط Synthetic SPX وإعادة تسعير Bid/Ask."
            if best
            else "لا يوجد عقد مستوفٍ لفلاتر السيولة والـGreeks وحداثة OPRA."
        )
        ai = self._preserved_ai_review(mode, technical)
        if allow_ai_review:
            ai = review_spx(
                self.db,
                self.settings,
                build_synthetic_review_payload(
                    synthetic, technical, scenario, news, ranked
                ),
            )
            if (
                ai.get("status") == "completed"
                and not ai.get("approved", True)
            ):
                decision = "no_trade"
                decision_ar = "لا صفقة"
                reason_ar = ai.get(
                    "explanation_ar",
                    "رفض المراجع القنصة الضعيفة.",
                )
                best = None
                market["contracts_actionable"] = False
        result = SPXHunterResult(
            generated_at=now,
            status="ready" if best else "no_trade",
            decision=decision,
            decision_ar=decision_ar,
            reason_ar=reason_ar,
            strike_mode=mode,
            capabilities=capabilities,
            market=market,
            synthetic=synthetic,
            technical=technical,
            news=news,
            news_impact_score=max(
                [int(item.get("spx_impact_score", 0)) for item in news]
                or [0]
            ),
            direction=direction,
            scenario=scenario,
            best_contract=best,
            ranked_contracts=ranked,
            rejected_contracts=rejected,
            ai_review=ai,
            warnings_ar=[
                "هذه قيمة ضمنية محسوبة من عقود Call وPut عبر OPRA، وليست قيمة SPX الرسمية المنشورة.",
                "دخول مشروط وليس سعرًا مضمونًا.",
                "Paper Trading فقط — التنفيذ الحقيقي محظور.",
            ],
        )
        return self._save(result)

    def _save(self, result: SPXHunterResult) -> dict:
        payload = result.model_dump(mode="json")
        now = datetime.now(timezone.utc)
        repository.cache_set(
            self.db, f"{CACHE_PREFIX}:{result.strike_mode.value}", payload,
            now + timedelta(minutes=5),
        )
        best = result.best_contract
        self.db.add(SPXHuntResult(
            market_state=str(result.market.get("stock_status", "unknown")),
            direction=result.direction.value,
            decision=result.decision,
            strike_mode=result.strike_mode.value,
            contract_symbol=best.symbol if best else None,
            strike=best.strike if best else None,
            dte=best.dte if best else None,
            entry=best.entry if best else None,
            stop=best.premium_stop_conservative if best else None,
            targets_json=best.target_scenarios if best else [],
            confidence_score=int((result.technical or {}).get("direction_clarity_score", 0)),
            escape_triggered=result.decision == "escape",
            payload_json=payload,
        ))
        self.db.commit()
        return payload

    def snapshot(self, mode: StrikeMode | str | None = None) -> dict:
        mode = StrikeMode(mode or self.settings.spx_default_strike_mode)
        cached = repository.cache_get_any(self.db, f"{CACHE_PREFIX}:{mode.value}")
        if cached:
            generated = datetime.fromisoformat(str(cached.get("generated_at")).replace("Z", "+00:00"))
            age = max(0, int((datetime.now(timezone.utc) - generated).total_seconds()))
            market = dict(cached.get("market") or {})
            data_age = market.get("data_age_seconds")
            effective_age = age + int(data_age or 0)
            if effective_age > self.settings.spx_max_data_age_seconds:
                market.update(
                    contracts_actionable=False,
                    monitoring_only=True,
                    data_age_seconds=effective_age,
                )
                if effective_age > self.settings.spx_direction_max_age_seconds:
                    cached = {
                        **cached,
                        "status": "stale",
                        "decision": "escape",
                        "decision_ar": "اهرب الآن",
                        "reason_ar": "قراءة اتجاه SPX تجاوزت حد الحداثة — ممنوع الدخول",
                        "market": market,
                        "best_contract": None,
                        "ranked_contracts": [],
                        "refresh_required": True,
                    }
                else:
                    market["direction_realtime"] = True
                    cached = {
                        **cached,
                        "status": "monitoring",
                        "decision": "wait",
                        "decision_ar": "راقب الاتجاه",
                        "reason_ar": (
                            "اتجاه SPX مباشر للمراقبة، وتسعير العقود يحتاج "
                            "تحديثًا أحدث قبل أي دخول."
                        ),
                        "market": market,
                        "best_contract": None,
                        "ranked_contracts": [],
                        "refresh_required": True,
                    }
            else:
                market["direction_realtime"] = True
                cached = {**cached, "market": market}
            return cached
        now = datetime.now(timezone.utc)
        session = market_session(now)
        return SPXHunterResult(
            generated_at=now, status="not_loaded", decision="wait",
            decision_ar="انتظر", reason_ar="اضغط تحديث للتحقق من بيانات SPX.",
            strike_mode=mode,
            capabilities=_unavailable_capability("لم يتم فحص المزود بعد"),
            market=serialize_market_session(session),
            refresh_required=True,
            warnings_ar=["Paper Trading ومراقبة فقط."],
        ).model_dump(mode="json")


class StaleSPXData(Exception):
    pass
