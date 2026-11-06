"""UGC-движок: создание карточек, like/dislike, автопромоушен, жалобы, premium-дни.

Чистый движок без сетевого I/O: состояние in-memory (карточки, история выдач,
голоса, жалобы). Rate/review-данные могут дублироваться в sqlite-адаптере —
здесь только правила.

- create_card: по умолчанию private draft; public ждёт автопромоушена;
- like/dislike: только тот, кому карточка реально выдалась (учёт выдач ведёт
  приложение через mark_issued; каждый участник пары — один голос);
- автопромоушен: 10 like и 0 dislike у public-карточки → status=pool,
  автору +1 день premium (grant_premium_day, накопительный premium_until);
- пост-модерация: ≥3 report → status=retired (движок выдачи draft/retired
  не отдаёт), каждый факт жалобы логируется.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from uuid import uuid4

from .models import Card, CardStatus, CardVisibility, Pair, SourceKind, User

logger = logging.getLogger(__name__)

# Порог автопромоушена: 10 like при 0 dislike.
PROMOTION_LIKES = 10
# Порог пост-модерации: 3 жалобы → retired.
RETIRE_REPORTS = 3
# Длительность гранта премиум-дня.
PREMIUM_DAY = timedelta(days=1)


class VoteKind(str, Enum):
    """Оценка выданной карточки (rating в спецификации)."""

    LIKE = "like"
    DISLIKE = "dislike"


class CardNotIssued(Exception):
    """Антифарм: голосующий не получал эту карточку (в любой паре)."""


class AlreadyVoted(Exception):
    """Антифарм: этот юзер уже голосовал за карточку — один голос на участника."""


class NotVotable(Exception):
    """Карточка retired — голоса больше не принимаются."""


@dataclass
class UgcState:
    """In-memory состояние UGC-движка."""

    cards: dict[str, Card] = field(default_factory=dict)  # id -> карточка
    issued_to: dict[str, set[str]] = field(default_factory=dict)  # id -> user_id получателей
    votes: dict[str, dict[str, VoteKind]] = field(default_factory=dict)  # id -> user_id -> голос
    reports: dict[str, set[str]] = field(default_factory=dict)  # id -> reporter_id
    authors: dict[str, User] = field(default_factory=dict)  # id -> автор (живой User)

    def like_count(self, card_id: str) -> int:
        return sum(v is VoteKind.LIKE for v in self.votes.get(card_id, {}).values())

    def dislike_count(self, card_id: str) -> int:
        return sum(v is VoteKind.DISLIKE for v in self.votes.get(card_id, {}).values())

    def report_count(self, card_id: str) -> int:
        return len(self.reports.get(card_id, ()))


def grant_premium_day(user: User, now: datetime | None = None) -> datetime:
    """+1 день премиума: накопительно к текущему premium_until.

    Если грант уже истёк — отсчёт от now; флаг is_premium ставится заново.
    Возвращает новый premium_until.
    """
    moment = now or datetime.now()
    base = user.premium_until
    if base is None or base <= moment:
        base = moment
    user.is_premium = True
    user.premium_until = base + PREMIUM_DAY
    return user.premium_until


class UgcEngine:
    """Правила UGC поверх in-memory состояния; выдачи регистрирует приложение."""

    def __init__(self, state: UgcState | None = None) -> None:
        self._state = state if state is not None else UgcState()

    @property
    def state(self) -> UgcState:
        return self._state

    def get(self, card_id: str) -> Card | None:
        return self._state.cards.get(card_id)

    # -- создание ------------------------------------------------------------

    def create_card(
        self,
        author: User,
        theme: str,
        text: str,
        visibility: CardVisibility = CardVisibility.PRIVATE,
    ) -> Card:
        """Новая UGC-карточка: private (только паре автора) или public-кандидат."""
        card = Card(
            id=f"ugc-{uuid4().hex}",
            theme=theme,
            text=text,
            source=SourceKind.UGC,
            author_id=author.id,
            visibility=visibility,
            status=CardStatus.DRAFT,
        )
        self._state.cards[card.id] = card
        self._state.authors[card.id] = author
        return card

    # -- учёт выдач (антифарм) -------------------------------------------------

    def mark_issued(self, pair: Pair, card: Card) -> None:
        """Регистрация выдачи: члены пары становятся единственными голосующими."""
        self._state.issued_to.setdefault(card.id, set()).update(u.id for u in pair.users)

    # -- голоса ----------------------------------------------------------------

    def like(self, voter: User, card: Card | str) -> Card:
        return self._vote(voter, card, VoteKind.LIKE)

    def dislike(self, voter: User, card: Card | str) -> Card:
        return self._vote(voter, card, VoteKind.DISLIKE)

    # -- жалобы (пост-модерация) ------------------------------------------------

    def report(self, reporter: User, card: Card | str) -> Card:
        """Жалоба: уникальные репортеры; ≥3 → retired + лог."""
        resolved = self._resolve(card)
        seen = self._state.reports.setdefault(resolved.id, set())
        seen.add(reporter.id)
        logger.info("report card=%r reporter=%r total=%d", resolved.id, reporter.id, len(seen))
        if len(seen) >= RETIRE_REPORTS and resolved.status is not CardStatus.RETIRED:
            resolved.status = CardStatus.RETIRED
            logger.warning("card %r retired: %d report(s)", resolved.id, len(seen))
        return resolved

    # -- внутреннее --------------------------------------------------------------

    def _resolve(self, card: Card | str) -> Card:
        card_id = card.id if isinstance(card, Card) else card
        resolved = self._state.cards.get(card_id)
        if resolved is None:
            raise KeyError(f"неизвестная карточка {card_id!r}")
        return resolved

    def _vote(self, voter: User, card: Card | str, kind: VoteKind) -> Card:
        resolved = self._resolve(card)
        receivers = self._state.issued_to.get(resolved.id, set())
        if voter.id not in receivers:
            raise CardNotIssued(
                f"user {voter.id!r} не получал карточку {resolved.id!r}"
            )
        if resolved.status is CardStatus.RETIRED:
            raise NotVotable(f"карточка {resolved.id!r} retired")

        votes = self._state.votes.setdefault(resolved.id, {})
        if voter.id in votes:
            raise AlreadyVoted(
                f"user {voter.id!r} уже голосовал за {resolved.id!r}"
            )
        votes[voter.id] = kind
        self._maybe_promote(resolved)
        return resolved

    def _maybe_promote(self, card: Card) -> None:
        """Автопромоушен: public + 10 like + 0 dislike → pool, автору premium-день."""
        if card.visibility is not CardVisibility.PUBLIC:
            return
        if card.status is not CardStatus.DRAFT:
            return
        if (
            self._state.like_count(card.id) >= PROMOTION_LIKES
            and self._state.dislike_count(card.id) == 0
        ):
            card.status = CardStatus.POOL
            author = self._state.authors[card.id]
            grant_premium_day(author)
            logger.info(
                "card %r promoted to pool: author %r gets premium day",
                card.id,
                author.id,
            )


__all__ = [
    "PREMIUM_DAY",
    "PROMOTION_LIKES",
    "RETIRE_REPORTS",
    "AlreadyVoted",
    "CardNotIssued",
    "NotVotable",
    "UgcEngine",
    "UgcState",
    "VoteKind",
    "grant_premium_day",
]
