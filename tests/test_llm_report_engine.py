from __future__ import annotations

import json
from datetime import date, datetime, timezone

import pytest

from app.engines.deterministic.schemas import (
    AccumulationDistribution,
    BollingerBands,
    DeterministicAnalysis,
    Indicators,
    Levels,
    Liquidity,
    MACDResult,
    MovingAverages,
    Regime,
    RSIDivergence,
    SMC,
    Scores,
    Volatility,
)
from app.engines.llm.adapters.base import LLMAdapter, LLMResponse
from app.engines.llm.report_engine import (
    build_fallback_report,
    find_banned_phrases,
    find_foreign_numbers,
    generate_report,
)


def _make_analysis(**overrides) -> DeterministicAnalysis:
    base = dict(
        symbol="NVDA",
        as_of=datetime.now(timezone.utc),
        data_as_of=date(2026, 1, 5),
        data_quality="daily_only",
        last_close=128.4,
        indicators=Indicators(
            rsi_14=58.3,
            macd=MACDResult(macd_line=1.2, signal_line=0.8, histogram=0.4, histogram_rising=True),
            vwap=None,
            atr_14=3.5,
            atr_pct=2.7,
            adx_14=24.0,
            moving_averages=MovingAverages(sma_20=None, sma_50=None, sma_200=None, ema_20=126.0, ema_50=122.0, ema_200=110.0),
            bollinger=BollingerBands(upper=134.0, middle=126.0, lower=118.0, bandwidth=0.06, bandwidth_percentile_90d=40.0),
        ),
        levels=Levels(supports=[], resistances=[], invalidation=118.5),
        volatility=Volatility(hv_20d=35.0, hv_60d=32.0, atr_pct_current=2.7, atr_pct_avg_90d=2.5, atr_pct_relative=1.08),
        liquidity=Liquidity(avg_volume_20d=5_000_000.0, rvol=1.1, dollar_volume=600_000_000.0, spread_pct=0.05, unusual_volume=False, unusual_volume_direction=None),
        regime=Regime(label="trending_up", reasons=["test"]),
        smc=SMC(
            rsi_divergence=RSIDivergence(detected=False, type=None, description=None),
            order_blocks=[],
            accumulation_distribution=AccumulationDistribution(current_value=1000.0, slope_20d=5.0, interpretation="accumulation"),
        ),
        scores=Scores(technical=68.0, volatility=45.0, liquidity=92.0, risk=38.0, overall_confidence=64.0),
    )
    base.update(overrides)
    return DeterministicAnalysis(**base)


class ScriptedAdapter(LLMAdapter):
    """Returns a scripted sequence of canned generate() responses, one per call."""

    def __init__(self, canned_texts: list[str]):
        self._responses = list(canned_texts)
        self.calls = 0

    def generate(self, system_prompt, user_content, max_tokens):
        text = self._responses[min(self.calls, len(self._responses) - 1)]
        self.calls += 1
        return LLMResponse(text=text, input_tokens=800, output_tokens=200, cost_usd=0.003)

    def extract_json_from_image(self, *args, **kwargs):
        raise NotImplementedError

    def estimate_cost(self, input_tokens, max_output_tokens):
        return 0.003

    def count_tokens(self, text):
        return len(text) // 4

    @property
    def provider_name(self):
        return "scripted"


def _valid_payload(analysis: DeterministicAnalysis) -> str:
    return json.dumps(
        {
            "tldr_ar": f"{analysis.symbol} عند {analysis.last_close} بثقة {analysis.scores.overall_confidence:.0f}.",
            "scenario_bullish_ar": "كسر فوق المقاومة يفتح المجال للصعود.",
            "scenario_bearish_ar": "كسر تحت الدعم يفتح المجال للهبوط.",
            "scenario_neutral_ar": "البقاء ضمن النطاق الحالي.",
            "full_report_ar": f"السهم {analysis.symbol} عند {analysis.last_close} بدرجة فنية {analysis.scores.technical:.0f}.",
            "devils_advocate_ar": "لم يرصد المحرك إنذارات بنيوية بارزة — وهذا بحد ذاته يستدعي الحذر من الرضا الزائد.",
        },
        ensure_ascii=False,
    )


def _bad_payload_ar(symbol_note: str = "") -> str:
    return json.dumps(
        {
            "tldr_ar": "رقم مخترع 555.55 هنا.",
            "scenario_bullish_ar": "x", "scenario_bearish_ar": "x", "scenario_neutral_ar": "x",
            "full_report_ar": "رقم مخترع 555.55 هنا.",
            "devils_advocate_ar": "لا إنذارات.",
        },
        ensure_ascii=False,
    )


# --- Numeric validation ---

def test_find_foreign_numbers_none_for_matching_values():
    analysis = _make_analysis()
    text = f"السعر {analysis.last_close} والدرجة الفنية {analysis.scores.technical:.0f}."
    assert find_foreign_numbers(text, analysis) == []


def test_find_foreign_numbers_flags_invented_value():
    analysis = _make_analysis()
    # Far from every fixture value (and outside any of their tolerance bands) on any scale.
    text = "السهم عند مستوى 12345.67 وهذا رقم غير موجود بالبيانات."
    foreign = find_foreign_numbers(text, analysis)
    assert 12345.67 in foreign


def test_find_foreign_numbers_tolerates_rounding():
    analysis = _make_analysis()
    # 128.4 rounded to 128 should be tolerated (abs tolerance 0.5)
    text = "السعر حوالي 128."
    assert find_foreign_numbers(text, analysis) == []


# --- Banned phrase check ---

def test_find_banned_phrases_detects_arabic_phrase():
    assert find_banned_phrases("هذه توصية بالشراء", "ar") == ["توصية"]


def test_find_banned_phrases_empty_for_clean_text():
    assert find_banned_phrases("مستوى فني ملحوظ عند 128", "ar") == []


# --- Fallback template ---

def test_build_fallback_report_contains_key_numbers_and_disclaimer():
    analysis = _make_analysis()
    result = build_fallback_report(analysis, alerts=["إنذار تجريبي"])
    assert "NVDA" in result.full_report
    assert "68" in result.full_report
    assert "الحذر من الرضا الزائد" not in result.full_report  # alerts list non-empty, so no fallback line
    assert "إنذار تجريبي" in result.devils_advocate_text
    assert result.tldr
    assert result.scenario_bullish and result.scenario_bearish and result.scenario_neutral
    from app.legal.disclaimers import BANNED_PHRASES_AR

    for phrase in BANNED_PHRASES_AR:
        assert phrase not in result.full_report


# --- generate_report end-to-end ---

def test_generate_report_happy_path_no_regeneration():
    analysis = _make_analysis()
    adapter = ScriptedAdapter([_valid_payload(analysis)])
    result = generate_report(adapter, analysis, alerts=[], lang="ar")
    assert result.used_fallback is False
    assert result.regenerated is False
    assert adapter.calls == 1
    assert "NVDA" in result.full_report


def test_generate_report_regenerates_once_after_foreign_number():
    analysis = _make_analysis()
    bad_payload = _bad_payload_ar()
    good_payload = _valid_payload(analysis)
    adapter = ScriptedAdapter([bad_payload, good_payload])
    result = generate_report(adapter, analysis, alerts=[], lang="ar")
    assert result.used_fallback is False
    assert result.regenerated is True
    assert adapter.calls == 2


def test_generate_report_falls_back_after_two_bad_attempts():
    analysis = _make_analysis()
    bad_payload = _bad_payload_ar()
    adapter = ScriptedAdapter([bad_payload, bad_payload])
    result = generate_report(adapter, analysis, alerts=[], lang="ar")
    assert result.used_fallback is True
    assert adapter.calls == 2
    assert result.total_cost_usd == pytest.approx(0.006)  # both failed attempts still cost money


def test_generate_report_falls_back_on_malformed_json():
    analysis = _make_analysis()
    adapter = ScriptedAdapter(["not json at all", "still not json"])
    result = generate_report(adapter, analysis, alerts=[], lang="ar")
    assert result.used_fallback is True


def test_build_fallback_report_english():
    analysis = _make_analysis()
    result = build_fallback_report(analysis, alerts=["test alert"], lang="en")
    assert "NVDA" in result.full_report
    assert "68" in result.full_report
    assert "test alert" in result.devils_advocate_text
    # The approved EN disclaimer legitimately contains "recommendations", which is why
    # find_banned_phrases (not a naive substring scan) is the real check here.
    assert find_banned_phrases(result.full_report, "en") == []


def test_generate_report_english_happy_path():
    analysis = _make_analysis()
    payload = json.dumps(
        {
            "tldr_en": f"{analysis.symbol} at {analysis.last_close}.",
            "scenario_bullish_en": "x", "scenario_bearish_en": "x", "scenario_neutral_en": "x",
            "full_report_en": f"{analysis.symbol} is trading at {analysis.last_close} with a technical score of {analysis.scores.technical:.0f}.",
            "devils_advocate_en": "No notable structural alerts were detected.",
        }
    )
    adapter = ScriptedAdapter([payload])
    result = generate_report(adapter, analysis, alerts=[], lang="en")
    assert result.used_fallback is False
    assert result.lang == "en"
    assert "NVDA" in result.full_report


def test_generate_report_falls_back_on_banned_phrase():
    analysis = _make_analysis()
    banned_payload = json.dumps(
        {
            "tldr_ar": "x", "scenario_bullish_ar": "x", "scenario_bearish_ar": "x", "scenario_neutral_ar": "x",
            "full_report_ar": f"هذه توصية بشراء السهم عند {analysis.last_close}.",
            "devils_advocate_ar": "لا إنذارات.",
        },
        ensure_ascii=False,
    )
    adapter = ScriptedAdapter([banned_payload, banned_payload])
    result = generate_report(adapter, analysis, alerts=[], lang="ar")
    assert result.used_fallback is True
