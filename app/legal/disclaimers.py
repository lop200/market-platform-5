"""Approved legal wording (SRS section 19). Single source of truth — never inline elsewhere."""
from __future__ import annotations

DISCLAIMER_AR = (
    "هذه المنصة أداة تحليل تعليمية لدعم القرار، وليست مستشاراً مالياً مرخصاً ولا تقدّم "
    "توصيات استثمارية. التداول بالأسهم والأوبشن ينطوي على مخاطر خسارة رأس المال كاملاً، "
    "والقرار ومسؤوليته يقعان على المستخدم وحده."
)

DISCLAIMER_EN = (
    "This platform is an educational analysis tool to support decisions. It is not a "
    "licensed financial advisor and does not provide investment recommendations. Trading "
    "stocks and options carries the risk of total capital loss. The decision and its "
    "responsibility rest solely with the user."
)

# SRS 19.1 — banned imperative/trade-signal wording, enforced by a post-generation filter
# in the LLM report engine (M2). Kept here so the list has one owner.
BANNED_PHRASES_AR: list[str] = [
    "اشترِ",
    "اشتري",
    "بيع الآن",
    "ادخل الآن",
    "ادخل عند",
    "توصية",
    "وقف خسارتك",
    "الهدف الأول",
    "هدف ربح",
    "مضمون",
    "مؤكد",
    # Widened for the Snipe scanner (SRS 19, CLAUDE.md rule 5): the bare imperative/noun
    # forms below were previously uncovered (only possessive/compound variants were
    # listed), and the scanner generates far more auto-text surface area than any prior
    # feature. Owner-approved widening — see 2026-07-17 conversation.
    "بِع",
    "وقف خسارة",
    "هدف",
    "دخول",
]

BANNED_PHRASES_EN: list[str] = [
    "buy now",
    "sell now",
    "enter now",
    "recommendation",
    "guaranteed",
    "sure thing",
    "stop loss",
    "entry point",
    "target",
]

# Screener feature (new, not in SRS) — fixed banner shown above every screener view (SRS 19
# wording pattern: ranking/scores are educational, never a trading invitation).
SCREENER_DISCLAIMER_AR = "ترتيب فني آلي لأغراض تعليمية — ليس دعوة للتداول."
SCREENER_DISCLAIMER_EN = "Automated technical ranking for educational purposes — not a trading invitation."

# Snipe scanner feature (new, not in SRS) — fixed banner shown above the "قنص اليوم" page
# (owner-specified literal wording, ties the ranking to the self-audit accuracy record).
SNIPE_DISCLAIMER_AR = (
    "ترتيب فني آلي بمعادلات معلنة لأغراض تعليمية وقياس دقة النظام — "
    "ليس دعوة للتداول والقرار مسؤولية المستخدم."
)
SNIPE_DISCLAIMER_EN = (
    "Automated technical ranking with declared formulas, for educational purposes and "
    "system accuracy measurement — not a trading invitation; the decision is the user's."
)
