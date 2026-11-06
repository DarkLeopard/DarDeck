"""RotationRule — RulePlugin: детерминированная ротация по истории пары.

Правило: сначала не показывавшиеся тексты (первый по порядку кандидатов),
иначе текст, который показывался давнее всех (LRU) — карточки «крутятся»,
а не повторяются вплотную.
"""

from __future__ import annotations

from typing import Sequence

from engine.interfaces import RulePlugin  # noqa: F401  (протокол pinned)
from engine.models import Card
from engine.policy import DeliveryPolicy


class RotationRule:
    """select(candidates, ...) -> карточка по стратегии ротации (LRU)."""

    def select(
        self,
        candidates: Sequence[Card],
        pair_history: Sequence[str],
        policy: DeliveryPolicy,
    ) -> Card:
        if not candidates:
            raise ValueError("RotationRule: пустой список кандидатов")
        # последняя позиция каждого текста в истории
        last_seen: dict[str, int] = {}
        for pos, text in enumerate(pair_history):
            last_seen[text] = pos
        unseen = [c for c in candidates if c.text not in last_seen]
        if unseen:
            return unseen[0]
        return min(candidates, key=lambda c: last_seen[c.text])
