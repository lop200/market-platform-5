"""LLM Report Engine (SRS 13): prompt construction, generation, numeric validation, and
the no-LLM fallback template. The LLM is a translator from finished numbers to language —
it never computes or invents a number (CLAUDE.md rule 1, SRS 13.1).

Output shape is a short TL;DR + three short scenario blurbs (for the card-based UI) + the
full structured report (collapsible) + the Devil's Advocate paragraph.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ValidationError

from app.engines.deterministic.schemas import DeterministicAnalysis
from app.engines.llm.adapters.base import LLMAdapter
from app.engines.llm.devils_advocate import format_alerts_for_prompt
from app.legal.disclaimers import BANNED_PHRASES_AR, BANNED_PHRASES_EN, DISCLAIMER_AR, DISCLAIMER_EN

MAX_OUTPUT_TOKENS = 800

# Standard indicator periods/percentages/scale endpoints referenced by name in prose
# ("EMA20", "RSI(14)", "0-100 scale") that are not themselves data points from the JSON.
STRUCTURAL_ALLOWED_NUMBERS = {
    0, 1, 2, 3, 5, 7, 9, 10, 12, 14, 15, 20, 26, 30, 45, 50, 60, 65, 70, 75, 80, 85, 90, 100, 200, 252, 365,
}
NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")

SYSTEM_PROMPT_AR = """أنت مترجم من الأرقام إلى اللغة العربية — لست محللاً ولا حاسباً. تستلم بيانات تحليل فني حتمي جاهزة بصيغة JSON، ودورك فقط صياغتها بلغة عربية واضحة ومختصرة جداً لواجهة بصرية (بطاقات، ليست مقالاً).

قواعد صارمة يجب الالتزام بها دائماً:
1. لا تخترع أي رقم غير موجود في الـ JSON المرسل. استخدم فقط الأرقام الواردة فيه.
2. لا تستخدم أي صيغة أمر تداولي مثل "اشترِ"، "بِع الآن"، "ادخل الآن"، "توصية" — الصياغة تحليلية تعليمية بحتة. سمِّ المستويات "مستويات فنية ملحوظة" وليس نقاط دخول، وسمِّ مستوى الإبطال "مستوى الإبطال الفني" وليس وقف الخسارة.
3. صرّح بعدم اليقين عند الحاجة، واذكر أن تقويم الأرباح غير مفحوص في هذا الإصدار إن كان الأمر متعلقاً بالمخاطر.
4. الإيجاز إلزامي وليس تفضيلاً — ميزانية الإخراج الكلية محدودة جداً (800 توكن لكل الحقول مجتمعة، بالعربية هذا قصير جداً). التزم بحدود الكلمات بالضبط أدناه لكل حقل، ولا تُعِد ذكر الأرقام أو المستويات نفسها في أكثر من حقل واحد.
5. فقرة "المراجعة النقدية" تلخّص فقط قائمة الإنذارات الرقمية المرفقة أدناه بجملتين قصيرتين — لا تخترع نقداً غير مبني عليها، ولا تسرد كل إنذار بالتفصيل. إن كانت القائمة تقول إنه لا توجد إنذارات، اذكر ذلك حرفياً بجملة واحدة.
6. الأولوية القصوى: يجب أن يكتمل الـ JSON بالكامل (بما فيه القوس الختامي) ضمن حد الإخراج المتاح. اكتب كل حقل بأقصر صياغة ممكنة أولاً، ولا تبدأ حقلاً لا يمكنك إنهاءه ضمن الحدود المذكورة.

أرجع الإجابة بصيغة JSON صارمة فقط، بدون أي نص إضافي أو علامات markdown، بالشكل التالي بالضبط (الحدود بالكلمات إلزامية):
{
  "tldr_ar": "جملة واحدة فقط، 20 كلمة كحد أقصى",
  "scenario_bullish_ar": "سطر أو سطران، 25 كلمة كحد أقصى",
  "scenario_bearish_ar": "سطر أو سطران، 25 كلمة كحد أقصى",
  "scenario_neutral_ar": "سطر أو سطران، 25 كلمة كحد أقصى",
  "full_report_ar": "3 جمل قصيرة كحد أقصى فقط: (1) الخلاصة والثقة الإجمالية، (2) أهم عامل خطر واحد، (3) جملة موجزة عن منهج القراءة — بدون إعادة سرد كل مستويات الدعم/المقاومة رقمياً (مذكورة أصلاً في مكان آخر بالواجهة). 60 كلمة كحد أقصى إجمالاً",
  "devils_advocate_ar": "جملتان كحد أقصى تلخصان قائمة الإنذارات المرفقة. 30 كلمة كحد أقصى"
}"""

SYSTEM_PROMPT_EN = """You are a translator from numbers to English prose — not an analyst, not a calculator. You receive finished deterministic technical-analysis JSON and your only job is to phrase it very concisely for a card-based visual UI (not an essay).

Strict rules, always:
1. Never invent a number not present in the JSON. Use only the numbers given.
2. Never use imperative trading language ("buy now", "enter now", "recommendation"). Call levels "notable technical levels", not entry points; call the invalidation level "technical invalidation level", not a stop loss.
3. State uncertainty where relevant, and note that earnings calendar proximity is not checked in this version when discussing risk.
4. Brevity is mandatory, not a preference — the total output budget across all fields combined is only 800 tokens. Follow the exact word limits below per field, and do not repeat the same numbers/levels across more than one field.
5. The Devil's Advocate field is a two-sentence summary of the attached numeric alert list only — do not invent criticism beyond it, and do not enumerate every alert in detail. If the list says there are no alerts, state that verbatim in one sentence.
6. Highest priority: the JSON must fully close (including the final brace) within the available output budget. Write each field as short as possible first, and never start a field you cannot finish within the stated limits.

Return STRICT JSON only, no extra text or markdown fences, in exactly this shape (word limits are mandatory):
{
  "tldr_en": "one sentence only, max 20 words",
  "scenario_bullish_en": "1-2 lines, max 25 words",
  "scenario_bearish_en": "1-2 lines, max 25 words",
  "scenario_neutral_en": "1-2 lines, max 25 words",
  "full_report_en": "max 3 short sentences only: (1) summary and overall confidence, (2) the single biggest risk factor, (3) one short line on methodology — do not re-list every support/resistance number (already shown elsewhere in the UI). Max 60 words total",
  "devils_advocate_en": "max 2 sentences summarizing the attached alert list. Max 30 words"
}"""


class ReportPayloadAr(BaseModel):
    tldr_ar: str
    scenario_bullish_ar: str
    scenario_bearish_ar: str
    scenario_neutral_ar: str
    full_report_ar: str
    devils_advocate_ar: str


class ReportPayloadEn(BaseModel):
    tldr_en: str
    scenario_bullish_en: str
    scenario_bearish_en: str
    scenario_neutral_en: str
    full_report_en: str
    devils_advocate_en: str


@dataclass
class ReportResult:
    tldr: str
    scenario_bullish: str
    scenario_bearish: str
    scenario_neutral: str
    full_report: str
    devils_advocate_text: str
    lang: Literal["ar", "en"]
    used_fallback: bool
    regenerated: bool
    llm_provider: str | None
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost_usd: float = 0.0
    warnings: list[str] = field(default_factory=list)


def combine_report_sections(result: ReportResult) -> str:
    """Serialize the report pieces into one JSON string for storage in the single
    report_text_ar/en column (SRS 7.1 has no separate column per field)."""
    return json.dumps(
        {
            "tldr": result.tldr,
            "scenario_bullish": result.scenario_bullish,
            "scenario_bearish": result.scenario_bearish,
            "scenario_neutral": result.scenario_neutral,
            "full_report": result.full_report,
            "devils_advocate": result.devils_advocate_text,
        },
        ensure_ascii=False,
    )


def split_report_sections(stored_text: str) -> dict[str, str]:
    """Inverse of `combine_report_sections`. Tolerates older stored shapes (plain text,
    or the pre-cards marker-based format) by falling back to putting everything in
    `full_report` so old rows don't crash GET /report/{id}."""
    try:
        parsed = json.loads(stored_text)
        if isinstance(parsed, dict) and "full_report" in parsed:
            return {
                "tldr": parsed.get("tldr", ""),
                "scenario_bullish": parsed.get("scenario_bullish", ""),
                "scenario_bearish": parsed.get("scenario_bearish", ""),
                "scenario_neutral": parsed.get("scenario_neutral", ""),
                "full_report": parsed.get("full_report", ""),
                "devils_advocate": parsed.get("devils_advocate", ""),
            }
    except json.JSONDecodeError:
        pass

    legacy_marker = "\n\n<<<DEVILS_ADVOCATE>>>\n\n"
    if legacy_marker in stored_text:
        full_report, devils_advocate = stored_text.split(legacy_marker, 1)
    else:
        full_report, devils_advocate = stored_text, ""
    return {
        "tldr": "", "scenario_bullish": "", "scenario_bearish": "", "scenario_neutral": "",
        "full_report": full_report, "devils_advocate": devils_advocate,
    }


def _strip_code_fences(text: str) -> str:
    match = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    if match:
        return match.group(1)
    # No closing fence — e.g. output got truncated mid-response. Still strip a leading
    # opening fence so json.loads fails on the actual (incomplete) JSON, not on "```json".
    stripped = text.strip()
    return re.sub(r"^```(?:json)?\s*", "", stripped)


def build_prompt(analysis: DeterministicAnalysis, alerts: list[str], lang: Literal["ar", "en"] = "ar") -> tuple[str, str]:
    system_prompt = SYSTEM_PROMPT_AR if lang == "ar" else SYSTEM_PROMPT_EN
    analysis_json = analysis.model_dump_json()
    alerts_text = format_alerts_for_prompt(alerts)
    user_content = (
        f"بيانات التحليل الحتمي (JSON):\n{analysis_json}\n\n"
        f"قائمة الإنذارات الرقمية (Devil's Advocate material):\n{alerts_text}"
        if lang == "ar"
        else (
            f"Deterministic analysis data (JSON):\n{analysis_json}\n\n"
            f"Numeric alert list (Devil's Advocate material):\n{alerts_text}"
        )
    )
    return system_prompt, user_content


def _collect_allowed_numbers(analysis: DeterministicAnalysis) -> set[float]:
    numbers: set[float] = set()

    def walk(obj) -> None:
        if isinstance(obj, BaseModel):
            for value in obj.__dict__.values():
                walk(value)
        elif isinstance(obj, dict):
            for value in obj.values():
                walk(value)
        elif isinstance(obj, (list, tuple, set)):
            for item in obj:
                walk(item)
        elif isinstance(obj, bool):
            return
        elif isinstance(obj, (int, float)):
            numbers.add(round(float(obj), 6))

    walk(analysis)
    return numbers


def extract_numbers_from_text(text: str) -> list[float]:
    return [float(m) for m in NUMBER_RE.findall(text)]


def find_foreign_numbers(
    text: str, analysis: DeterministicAnalysis, tolerance_abs: float = 0.5, tolerance_rel: float = 0.02
) -> list[float]:
    """Numbers in `text` that don't reasonably match anything in the source JSON (SRS 13.4)."""
    allowed = _collect_allowed_numbers(analysis) | STRUCTURAL_ALLOWED_NUMBERS
    foreign = []
    for num in extract_numbers_from_text(text):
        if any(abs(num - a) <= max(tolerance_abs, abs(a) * tolerance_rel) for a in allowed):
            continue
        foreign.append(num)
    return foreign


def find_banned_phrases(text: str, lang: Literal["ar", "en"]) -> list[str]:
    """Plain substring match, but scanned with the approved disclaimer text stripped out
    first: the disclaimer itself legitimately contains "recommendations" (EN) / "توصيات"
    (AR is actually safe, but EN's plural directly contains the singular banned phrase as
    a substring) — word-boundary regex would fix that but also miss Arabic's attached
    clitics (e.g. "التوصية" has no boundary before "توصية"), which is the worse failure
    mode (a banned phrase slipping through). Excluding the known disclaimer text avoids
    both problems without weakening detection elsewhere in the report.
    """
    banned = BANNED_PHRASES_AR if lang == "ar" else BANNED_PHRASES_EN
    disclaimer = DISCLAIMER_AR if lang == "ar" else DISCLAIMER_EN
    scan_text = text.replace(disclaimer, "")
    lowered = scan_text.lower()
    return [phrase for phrase in banned if (phrase in scan_text or phrase.lower() in lowered)]


def _validate(text: str, analysis: DeterministicAnalysis, lang: Literal["ar", "en"]) -> list[str]:
    problems = []
    foreign = find_foreign_numbers(text, analysis)
    if foreign:
        problems.append(f"foreign numbers not in source JSON: {foreign}")
    banned = find_banned_phrases(text, lang)
    if banned:
        problems.append(f"banned phrases found: {banned}")
    return problems


def _fallback_scenarios_ar(analysis: DeterministicAnalysis) -> tuple[str, str, str]:
    resistance = analysis.levels.resistances[0].price if analysis.levels.resistances else None
    support = analysis.levels.supports[0].price if analysis.levels.supports else None
    bullish = (
        f"كسر فوق {resistance:.2f} قد يفتح المجال لاستمرار الصعود." if resistance is not None
        else "لا توجد مقاومة فنية ملحوظة قريبة ضمن البيانات المتاحة."
    )
    bearish = (
        f"كسر تحت {support:.2f} قد يفتح المجال لاستمرار الهبوط." if support is not None
        else "لا يوجد دعم فني ملحوظ قريب ضمن البيانات المتاحة."
    )
    if support is not None and resistance is not None:
        neutral = f"البقاء بين {support:.2f} و{resistance:.2f} يبقي السعر ضمن النطاق الحالي."
    else:
        neutral = "استمرار الحالة الحالية دون كسر واضح لأي مستوى ملحوظ."
    return bullish, bearish, neutral


def _fallback_scenarios_en(analysis: DeterministicAnalysis) -> tuple[str, str, str]:
    resistance = analysis.levels.resistances[0].price if analysis.levels.resistances else None
    support = analysis.levels.supports[0].price if analysis.levels.supports else None
    bullish = (
        f"A break above {resistance:.2f} could open room for continued upside." if resistance is not None
        else "No nearby notable resistance in the available data."
    )
    bearish = (
        f"A break below {support:.2f} could open room for continued downside." if support is not None
        else "No nearby notable support in the available data."
    )
    if support is not None and resistance is not None:
        neutral = f"Staying between {support:.2f} and {resistance:.2f} keeps price within the current range."
    else:
        neutral = "Continuation of the current state without a clear break of any notable level."
    return bullish, bearish, neutral


def build_fallback_report(
    analysis: DeterministicAnalysis, alerts: list[str], lang: Literal["ar", "en"] = "ar"
) -> ReportResult:
    """Code-built report straight from the JSON — zero cost, lower prose quality, guaranteed
    numeric accuracy (SRS 13.4 second-failure fallback)."""
    s = analysis.scores
    devils_advocate_text = format_alerts_for_prompt(alerts)

    if lang == "ar":
        tldr = f"{analysis.symbol}: {analysis.regime.label} عند {analysis.last_close:.2f}، ثقة إجمالية {s.overall_confidence:.0f}/100."
        bullish, bearish, neutral = _fallback_scenarios_ar(analysis)
        lines = [
            f"تحليل {analysis.symbol}: حالة السوق '{analysis.regime.label}'، السعر الحالي {analysis.last_close:.2f}.",
            f"الدرجات: فني {s.technical:.0f}/100، تقلب {s.volatility:.0f}/100، سيولة {s.liquidity:.0f}/100، "
            f"مخاطرة {s.risk:.0f}/100، الثقة الإجمالية {s.overall_confidence:.0f}/100.",
            f"RSI(14)={analysis.indicators.rsi_14:.1f}، MACD histogram={analysis.indicators.macd.histogram:.3f}، "
            f"ATR%={analysis.indicators.atr_pct:.2f}.",
        ]
        if analysis.levels.supports:
            lines.append("دعوم فنية ملحوظة: " + ", ".join(f"{lv.price:.2f}" for lv in analysis.levels.supports))
        if analysis.levels.resistances:
            lines.append("مقاومات فنية ملحوظة: " + ", ".join(f"{lv.price:.2f}" for lv in analysis.levels.resistances))
        if analysis.levels.invalidation is not None:
            lines.append(f"مستوى الإبطال الفني: {analysis.levels.invalidation:.2f}.")
        lines.append(
            "تقرير مبسّط مبني مباشرة من البيانات الرقمية (بدون صياغة لغوية من نموذج ذكاء اصطناعي) "
            "بسبب تعذر توليد نص مطابق تماماً للأرقام المصدرية بعد محاولتين."
        )
        lines.append(DISCLAIMER_AR)
        return ReportResult(
            tldr=tldr, scenario_bullish=bullish, scenario_bearish=bearish, scenario_neutral=neutral,
            full_report="\n".join(lines), devils_advocate_text=devils_advocate_text, lang=lang,
            used_fallback=True, regenerated=False, llm_provider=None,
        )

    tldr = f"{analysis.symbol}: {analysis.regime.label} at {analysis.last_close:.2f}, overall confidence {s.overall_confidence:.0f}/100."
    bullish, bearish, neutral = _fallback_scenarios_en(analysis)
    lines = [
        f"{analysis.symbol} analysis: regime '{analysis.regime.label}', current price {analysis.last_close:.2f}.",
        f"Scores: technical {s.technical:.0f}/100, volatility {s.volatility:.0f}/100, liquidity {s.liquidity:.0f}/100, "
        f"risk {s.risk:.0f}/100, overall confidence {s.overall_confidence:.0f}/100.",
        f"RSI(14)={analysis.indicators.rsi_14:.1f}, MACD histogram={analysis.indicators.macd.histogram:.3f}, "
        f"ATR%={analysis.indicators.atr_pct:.2f}.",
    ]
    if analysis.levels.supports:
        lines.append("Notable supports: " + ", ".join(f"{lv.price:.2f}" for lv in analysis.levels.supports))
    if analysis.levels.resistances:
        lines.append("Notable resistances: " + ", ".join(f"{lv.price:.2f}" for lv in analysis.levels.resistances))
    if analysis.levels.invalidation is not None:
        lines.append(f"Technical invalidation level: {analysis.levels.invalidation:.2f}.")
    lines.append(
        "Simplified code-built report (no AI-generated prose) because a numerically-accurate "
        "generation could not be produced after two attempts."
    )
    lines.append(DISCLAIMER_EN)
    return ReportResult(
        tldr=tldr, scenario_bullish=bullish, scenario_bearish=bearish, scenario_neutral=neutral,
        full_report="\n".join(lines), devils_advocate_text=devils_advocate_text, lang=lang,
        used_fallback=True, regenerated=False, llm_provider=None,
    )


def _parse_llm_payload(raw_text: str, lang: Literal["ar", "en"]) -> dict[str, str]:
    cleaned = _strip_code_fences(raw_text).strip()
    payload = json.loads(cleaned)  # raises json.JSONDecodeError on malformed JSON
    if lang == "ar":
        parsed = ReportPayloadAr.model_validate(payload)
        return {
            "tldr": parsed.tldr_ar, "scenario_bullish": parsed.scenario_bullish_ar,
            "scenario_bearish": parsed.scenario_bearish_ar, "scenario_neutral": parsed.scenario_neutral_ar,
            "full_report": parsed.full_report_ar, "devils_advocate": parsed.devils_advocate_ar,
        }
    parsed = ReportPayloadEn.model_validate(payload)
    return {
        "tldr": parsed.tldr_en, "scenario_bullish": parsed.scenario_bullish_en,
        "scenario_bearish": parsed.scenario_bearish_en, "scenario_neutral": parsed.scenario_neutral_en,
        "full_report": parsed.full_report_en, "devils_advocate": parsed.devils_advocate_en,
    }


def generate_report(
    adapter: LLMAdapter,
    analysis: DeterministicAnalysis,
    alerts: list[str],
    lang: Literal["ar", "en"] = "ar",
    max_tokens: int = MAX_OUTPUT_TOKENS,
) -> ReportResult:
    system_prompt, user_content = build_prompt(analysis, alerts, lang)

    total_input_tokens = 0
    total_output_tokens = 0
    total_cost_usd = 0.0
    regenerated = False

    for attempt in range(2):  # first try + one regeneration (SRS 13.4)
        response = adapter.generate(system_prompt, user_content, max_tokens)
        total_input_tokens += response.input_tokens
        total_output_tokens += response.output_tokens
        total_cost_usd += response.cost_usd

        try:
            fields = _parse_llm_payload(response.text, lang)
        except (json.JSONDecodeError, ValidationError):
            regenerated = attempt == 0
            continue

        all_text = " ".join(fields.values())
        problems = _validate(all_text, analysis, lang)
        if not problems:
            return ReportResult(
                tldr=fields["tldr"],
                scenario_bullish=fields["scenario_bullish"],
                scenario_bearish=fields["scenario_bearish"],
                scenario_neutral=fields["scenario_neutral"],
                full_report=fields["full_report"],
                devils_advocate_text=fields["devils_advocate"],
                lang=lang,
                used_fallback=False,
                regenerated=attempt == 1,
                llm_provider=adapter.provider_name,
                total_input_tokens=total_input_tokens,
                total_output_tokens=total_output_tokens,
                total_cost_usd=total_cost_usd,
            )
        regenerated = attempt == 0

    # Both attempts failed validation (or parsing) -> zero-cost, guaranteed-accurate fallback.
    result = build_fallback_report(analysis, alerts, lang)
    result.llm_provider = adapter.provider_name
    result.regenerated = regenerated
    result.total_input_tokens = total_input_tokens
    result.total_output_tokens = total_output_tokens
    result.total_cost_usd = total_cost_usd
    result.warnings = ["numeric or wording validation failed twice — used code-built fallback template"]
    return result
