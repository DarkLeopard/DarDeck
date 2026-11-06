"""RandomRule — RulePlugin: равновероятный выбор из кандидатов.

rng можно передать (random.Random(seed)) для детерминированных тестов.
"""

from __future__ import annotations

import random
from typing import Sequence

from engine.interfaces import RulePlugin  # noqa: F401  (протокол pinned)
from engine.models import Card
from engine.policy import DeliveryPolicy


class RandomRule:
    """select(candidates, ...) -> случайный кандидат из списка."""

    def __init__(self, rng: random.Random | None = None) -> None:
        self._rng = rng if rng is not None else random.Random()

    def select(
        self,
        candidates: Sequence[Card],
        pair_history: Sequence[str],
        policy: DeliveryPolicy,
    ) -> Card:
        if not candidates:
            raise ValueError("RandomRule: пустой список кандидатов")
        return self._rng.choice(list(candidates))
