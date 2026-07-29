from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SocialSignal:
    mention_velocity: float | None = None
    sentiment: str = "غير متوفر"
    promotional_risk: bool = False


class DisabledSocialSentimentProvider:
    provider_name = "disabled"

    def get_signal(self, symbol: str) -> SocialSignal:
        return SocialSignal()
