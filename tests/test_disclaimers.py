"""Coverage for the widened banned-phrase filter (SRS 19, CLAUDE.md rule 5) and the new
Snipe disclaimer — the Snipe scanner generates far more auto-text surface area than any
prior feature, so the previously-uncovered bare imperative/noun forms needed closing."""
from __future__ import annotations

from app.engines.llm.report_engine import find_banned_phrases
from app.legal.disclaimers import SNIPE_DISCLAIMER_AR, SNIPE_DISCLAIMER_EN


def test_previously_uncovered_bare_forms_are_now_banned():
    assert find_banned_phrases("لا تنسَ أن بِع عند الحاجة", "ar") != []
    assert find_banned_phrases("ضع وقف خسارة قريباً من السعر", "ar") != []
    assert find_banned_phrases("الهدف عند 150 دولار", "ar") != []
    assert find_banned_phrases("نقطة دخول جيدة هنا", "ar") != []


def test_previously_covered_phrases_still_banned():
    assert find_banned_phrases("اشترِ الآن", "ar") != []
    assert find_banned_phrases("توصية اليوم", "ar") != []
    assert find_banned_phrases("buy now", "en") != []
    assert find_banned_phrases("this is a recommendation", "en") != []


def test_new_english_bare_forms_are_banned():
    assert find_banned_phrases("set your stop loss here", "en") != []
    assert find_banned_phrases("the entry point is 150", "en") != []
    assert find_banned_phrases("price target is 150", "en") != []


def test_snipe_disclaimer_itself_passes_the_filter():
    assert find_banned_phrases(SNIPE_DISCLAIMER_AR, "ar") == []
    assert find_banned_phrases(SNIPE_DISCLAIMER_EN, "en") == []
