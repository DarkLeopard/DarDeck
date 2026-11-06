"""Ядро движка: issue_card(pair) — выдача карточки паре без сетевых зависимостей.

- дедуп: не та же карточка, что уже показывалась паре (ключ — поле text);
- приоритет: pool (UGC в пуле), потом seed;
- спонсорские: ≤1 на 7 показов в паре;
- учёт issued/completed в in-memory состоянии движка по паре (MVP).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from .interfaces import CardSource, RulePlugin
from .models import Card, CardStatus, Pair, SourceKind
from .policy import DeliveryPolicy

# Капа спонсорских: ≤1 на N показов в паре.
SPONSORED_CYCLE = 7


class NoCardAvailable(Exception):
    """Нет карточки, которую можно выдать паре (всё показано или заблокировано капой)."""


@dataclass
class PairState:
    """In-memory учёт показов движка по паре."""

    history: list[str] = field(default_factory=list)  # тексты показанных карточек, по порядку
    issued: set[str] = field(default_factory=set)  # id выданных карточек
    completed: set[str] = field(default_factory=set)  # id завершённых карточек
    # флаги спонсорскости последних показов; окно 6: перед новой спонсорской
    # в последних 6 показах её быть не должно (иначе окно 7 нарушит капу)
    sponsored_window: deque[bool] = field(default_factory=lambda: deque(maxlen=SPONSORED_CYCLE - 1))


def _is_eligible(card: Card) -> bool:
    """К выдаче пригодны seed-карточки и UGC-карточки в пуле; draft/retired — нет."""
    return card.source == SourceKind.SEED or card.status == CardStatus.POOL


class Engine:
    """Чистая библиотека: selection через CardSource + RulePlugin, учёт здесь."""

    def __init__(
        self,
        source: CardSource,
        rule: RulePlugin,
        policy: DeliveryPolicy | None = None,
    ) -> None:
        self._source = source
        self._rule = rule
        self._policy = policy if policy is not None else DeliveryPolicy()
        self._states: dict[str, PairState] = {}

    @property
    def policy(self) -> DeliveryPolicy:
        return self._policy

    def pair_state(self, pair: Pair) -> PairState:
        return self._states.setdefault(pair.id, PairState())

    def issue_card(self, pair: Pair) -> Card:
        """Выдать пару новую карточку; если вариантов нет — NoCardAvailable."""
        state = self.pair_state(pair)
        candidates = self._source.fetch_candidates(pair, self._policy)
        seen = set(state.history)

        eligible = [c for c in candidates if _is_eligible(c) and c.text not in seen]
        if not self._sponsored_allowed(state):
            eligible = [c for c in eligible if not c.is_sponsored]
        if not eligible:
            raise NoCardAvailable(f"pair {pair.id!r}: нет доступных карточек")

        # приоритет pool (UGC) -> seed
        ordered = [c for c in eligible if c.source == SourceKind.UGC]
        ordered += [c for c in eligible if c.source == SourceKind.SEED]

        card = self._rule.select(ordered, tuple(state.history), self._policy)
        self._record(state, card)
        return card

    def mark_completed(self, pair: Pair, card: Card | str) -> None:
        """Учёт completed: карточка пройдена парой."""
        card_id = card.id if isinstance(card, Card) else card
        self.pair_state(pair).completed.add(card_id)

    # -- внутреннее ---------------------------------------------------------

    def _sponsored_allowed(self, state: PairState) -> bool:
        # Новая спонсорская допустима, только если среди последних (до 6) показов
        # пары не было другой: тогда в любом окне из 7 показов её максимум одна.
        return not any(state.sponsored_window)

    def _record(self, state: PairState, card: Card) -> None:
        state.history.append(card.text)
        state.issued.add(card.id)
        state.sponsored_window.append(card.is_sponsored)
