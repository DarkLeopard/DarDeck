"""Протоколы контрактов движка (pinned, должны совпадать с именами спецификации).

- CardSource.fetch_candidates(pair, policy) -> list[Card]
- RulePlugin.select(candidates, pair_history, policy) -> Card

Реализации живут в адаптерах (Task 3); движок зависит только от протоколов.
"""

from __future__ import annotations

from typing import Protocol, Sequence

from .models import Card, Pair
from .policy import DeliveryPolicy


class CardSource(Protocol):
    """Адаптер поверх произвольного хранилища карточек."""

    def fetch_candidates(self, pair: Pair, policy: DeliveryPolicy) -> list[Card]:
        """Кандидаты для выдачи паре (все статусы; фильтрацию делает движок)."""
        ...


class RulePlugin(Protocol):
    """Стратегия выбора карточки из кандидатов."""

    def select(
        self,
        candidates: Sequence[Card],
        pair_history: Sequence[str],
        policy: DeliveryPolicy,
    ) -> Card:
        """Выбрать одну карточку. pair_history — тексты уже показанных карточек."""
        ...
