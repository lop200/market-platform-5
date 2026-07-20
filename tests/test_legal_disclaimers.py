from __future__ import annotations

from app.legal.disclaimers import (
    BANNED_PHRASES_AR,
    BANNED_PHRASES_EN,
    DISCLAIMER_AR,
    DISCLAIMER_EN,
)


def test_disclaimers_are_non_empty():
    assert len(DISCLAIMER_AR) > 20
    assert len(DISCLAIMER_EN) > 20


def test_banned_phrase_lists_cover_srs_examples():
    assert "توصية" in BANNED_PHRASES_AR
    assert "اشترِ" in BANNED_PHRASES_AR
    assert "recommendation" in BANNED_PHRASES_EN
