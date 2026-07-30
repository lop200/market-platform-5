from __future__ import annotations

import re
from difflib import SequenceMatcher

from app.news.schemas import NewsEvent


EVENT_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("reverse_split", ("reverse split", "reverse stock split")),
    ("offering", ("public offering", "registered direct", "secondary offering", "424b")),
    ("dilution", ("dilution", "dilutive", "share issuance")),
    ("atm", ("at-the-market", "at the market offering", "atm program")),
    ("halt", ("trading halt", "halted")),
    ("guidance", ("guidance", "outlook", "forecast")),
    ("earnings", ("earnings", "quarterly results", "financial results", "10-q", "10-k")),
    ("analyst_upgrade", ("upgrade", "raises rating", "price target raised")),
    ("analyst_downgrade", ("downgrade", "cuts rating", "price target cut")),
    ("merger", ("merger",)),
    ("acquisition", ("acquire", "acquisition", "to buy")),
    ("lawsuit", ("lawsuit", "litigation", "investigation", "subpoena")),
    ("insider_buy", ("insider buy", "form 4 purchase")),
    ("insider_sell", ("insider sell", "form 4 sale")),
    ("fda", ("fda", "food and drug administration")),
    ("fed", ("federal reserve", "fed chair", "fomc")),
    ("inflation", ("cpi", "pce", "inflation")),
    ("employment", ("payrolls", "unemployment", "jobs report", "employment")),
    ("interest_rates", ("interest rate", "rate cut", "rate hike", "treasury yield")),
    ("geopolitical", ("war", "sanctions", "geopolitical", "military strike")),
    ("product_launch", ("product launch", "launches", "unveils")),
    ("management_change", ("chief executive", "ceo resign", "cfo resign", "appoints ceo")),
    ("rumor", ("rumor", "unconfirmed", "reportedly", "sources say")),
    ("sec_filing", ("8-k", "s-1", "form 4", "sec filing")),
]

HIGH_RISK = {"offering", "dilution", "atm", "reverse_split", "halt"}
MARKET_EVENTS = {"macro", "fed", "inflation", "employment", "interest_rates", "geopolitical"}


def normalize_headline(value: str) -> str:
    return re.sub(r"[^a-z0-9\u0600-\u06ff]+", " ", value.lower()).strip()


def classify_event(text: str, *, filing_form: str | None = None) -> str:
    normalized = normalize_headline(f"{filing_form or ''} {text}")
    form = (filing_form or "").upper()
    if form.startswith("424B"):
        return "offering"
    if form in {"8-K", "10-Q", "10-K", "S-1", "FORM 4", "4"}:
        if form in {"10-Q", "10-K"} and "results" in normalized:
            return "earnings"
        return "sec_filing"
    for event_type, phrases in EVENT_RULES:
        if any(phrase in normalized for phrase in phrases):
            return event_type
    return "other"


def score_event(event_type: str, *, official: bool, source_type: str) -> tuple[str, int, int, list[str]]:
    negative = {
        "offering", "dilution", "atm", "reverse_split", "halt", "lawsuit",
        "analyst_downgrade", "insider_sell",
    }
    positive = {"analyst_upgrade", "insider_buy", "product_launch", "acquisition"}
    sentiment = "negative" if event_type in negative else "positive" if event_type in positive else "neutral"
    base = {
        "halt": 98, "offering": 92, "dilution": 92, "atm": 88,
        "reverse_split": 86, "fed": 90, "inflation": 88, "employment": 82,
        "interest_rates": 90, "earnings": 85, "guidance": 85,
        "lawsuit": 75, "management_change": 70, "rumor": 65,
    }.get(event_type, 55)
    impact = min(100, base + (5 if official else -10))
    urgency = min(100, impact + (5 if event_type in HIGH_RISK else 0))
    flags = [event_type] if event_type in HIGH_RISK | {"lawsuit", "rumor", "guidance"} else []
    if source_type == "x" and not official:
        flags.append("unconfirmed")
    return sentiment, impact, urgency, flags


def apply_safety(event: NewsEvent, *, analysis_direction: str | None = None, analysis_issued_at=None) -> NewsEvent:
    risky_official = event.is_official and (
        event.event_type in HIGH_RISK
        or (event.event_type == "lawsuit" and event.impact_score >= 80)
    )
    event.prevent_entry = risky_official
    event.raise_risk = event.impact_score >= 70 or event.event_type in {"rumor", "guidance"}
    if analysis_issued_at is not None:
        event.invalidates_previous_analysis = (
            event.published_at > analysis_issued_at and event.impact_score >= 75
        )
    if analysis_direction:
        bullish = any(token in analysis_direction.lower() for token in ("bull", "صاعد"))
        event.supports_technical_scenario = (
            event.sentiment == "positive" and bullish
        ) or (event.sentiment == "negative" and not bullish)
        event.contradicts_technical_scenario = (
            event.sentiment == "negative" and bullish
        ) or (event.sentiment == "positive" and not bullish)
    event.relation_reason_ar = (
        "حدث رسمي قد يغيّر صلاحية الفرصة ويمنع الدخول الجديد."
        if event.prevent_entry
        else "خبر مرتفع التأثير يرفع المخاطرة ويحتاج تأكيدًا فنيًا جديدًا."
        if event.raise_risk
        else "يُستخدم كسياق مساعد فقط مع التحليل الفني والسيولة."
    )
    if event.reliability_score < 60 or event.event_type == "rumor":
        event.status_message_ar = "خبر غير مؤكد — لا يعتمد عليه للتنفيذ"
    elif event.conflict_warning:
        event.status_message_ar = "مصادر متعارضة — انتظر التأكيد"
    elif event.age_seconds > 86_400:
        event.status_message_ar = "الخبر قديم ولا يدخل في التحليل الحالي"
    return event


def deduplicate(events: list[NewsEvent], *, window_seconds: int = 10_800) -> list[NewsEvent]:
    ranked = sorted(events, key=lambda x: (x.reliability_score, x.is_official, -x.age_seconds), reverse=True)
    kept: list[NewsEvent] = []
    for event in ranked:
        duplicate = None
        normalized = normalize_headline(event.headline)
        for candidate in kept:
            close_time = abs((event.published_at - candidate.published_at).total_seconds()) <= window_seconds
            same_symbol = bool(set(event.symbols) & set(candidate.symbols)) or not event.symbols or not candidate.symbols
            same_url = bool(event.source_url and event.source_url == candidate.source_url)
            similarity = SequenceMatcher(None, normalized, normalize_headline(candidate.headline)).ratio()
            if same_url or (close_time and same_symbol and event.event_type == candidate.event_type and similarity >= 0.82):
                duplicate = candidate
                break
        if duplicate:
            event.is_duplicate = True
            duplicate.confirming_sources.append({
                "source_name": event.source_name,
                "source_url": event.source_url,
                "reliability_score": event.reliability_score,
            })
            if event.sentiment != "neutral" and duplicate.sentiment != "neutral" and event.sentiment != duplicate.sentiment:
                duplicate.conflict_warning = True
                duplicate.status_message_ar = "مصادر متعارضة — انتظر التأكيد"
            continue
        kept.append(event)
    return sorted(kept, key=lambda x: (x.published_at, x.impact_score), reverse=True)
