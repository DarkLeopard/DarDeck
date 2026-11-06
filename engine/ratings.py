"""Ratings/reviews: оценка +1/0/-1, опциональный текст-отзыв; среднее и количество.

Чистый движок без сетевого I/O и БД: журнал оценок — in-memory состояние
RatingEngine (сериализуемый список записей to_records/from_records).

Интеграция с engine.ugc: голоса лайков/дизлайков — СВОЁ состояние ugc (votes
со своей пороговой логикой промоушена/ретира); журнал ratings.py его не
дублирует и пороговой логики ugc не повторяет. Оценку ставит получатель
карточки (после /done), шкала другая — +1/0/-1 с текст-отзывом.
Порог review-листа (среднее <0 при >=5 оценках) — константа/функция здесь,
используются tools/cleanup.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .models import Card, User

# Шкала оценки (rating в спецификации): +1/0/-1; 0 — нейтральная без лайка.
MIN_VALUE = -1
NEUTRAL_VALUE = 0
MAX_VALUE = 1

# Порог review-листа: карточка попадает в список при >=5 оценках.
REVIEW_MIN_COUNT = 5


class RatingError(Exception):
    """Базовая ошибка оценки."""


class BadRatingValue(RatingError):
    """Оценка не +1/0/-1."""


class AlreadyRated(RatingError):
    """Этот юзер уже оценил карточку — одна оценка на участника."""


def is_for_review(average: float, count: int) -> bool:
    """Порог review-листа cleanup'а: среднее <0 при >=5 оценках."""
    return count >= REVIEW_MIN_COUNT and average < 0


@dataclass(frozen=True)
class Rating:
    """Одна оценка: юзер, значение +1/0/-1, текст-отзыв (опционально)."""

    user_id: str
    value: int
    text: str | None = None


@dataclass
class CardRating:
    """Сводка по карточке: средняя оценка и количество оценок."""

    average: float = 0.0
    count: int = 0


@dataclass
class RatingJournal:
    """Журнал: card_id -> user_id -> Rating; сводные средние по карточкам."""

    entries: dict[str, dict[str, Rating]] = field(default_factory=dict)
    summaries: dict[str, CardRating] = field(default_factory=dict)

    def summary(self, card_id: str) -> CardRating:
        return self.summaries.get(card_id, CardRating())


def _card_id(card: Card | str) -> str:
    return card.id if isinstance(card, Card) else card


def _user_id(user: User | str) -> str:
    return user.id if isinstance(user, User) else user


class RatingEngine:
    """Журнал оценок/отзывов поверх in-memory состояния; хранит среднее и количество."""

    def __init__(self, journal: RatingJournal | None = None) -> None:
        self._journal = journal if journal is not None else RatingJournal()

    @property
    def journal(self) -> RatingJournal:
        return self._journal

    def rate(
        self,
        user: User | str,
        card: Card | str,
        value: int,
        text: str | None = None,
    ) -> Rating:
        """Оценить карточку: +1/0/-1 и опциональный текст-отзыв.

        Одна оценка на юзера на карточку (повтор — AlreadyRated, зеркально ugc).
        Среднее и количество по карточке пересчитываются здесь же.
        """
        cid = _card_id(card)
        uid = _user_id(user)
        if value not in (MIN_VALUE, NEUTRAL_VALUE, MAX_VALUE):
            raise BadRatingValue(f"оценка {value!r}: шкала +1/0/-1")
        per_card = self._journal.entries.setdefault(cid, {})
        if uid in per_card:
            raise AlreadyRated(f"user {uid!r} уже оценил карточку {cid!r}")
        rating = Rating(user_id=uid, value=value, text=text)
        per_card[uid] = rating
        self._recount(cid)
        return rating

    # -- сводные по карточке (хранятся в журнале) ----------------------------

    def summary(self, card: Card | str) -> CardRating:
        """Среднее и количество по карточке (без оценок — 0.0/0)."""
        return self._journal.summary(_card_id(card))

    def average(self, card: Card | str) -> float:
        """Средняя оценка карточки (0.0, если оценок нет)."""
        return self.summary(card).average

    def count(self, card: Card | str) -> int:
        """Количество оценок карточки."""
        return self.summary(card).count

    def reviews(self, card: Card | str) -> list[Rating]:
        """Все оценки карточки (как выставлены)."""
        return list(self._journal.entries.get(_card_id(card), {}).values())

    def review_texts(self, card: Card | str) -> list[str]:
        """Тексты-отзывы карточки (оценки без текста не входят)."""
        return [r.text for r in self.reviews(card) if r.text is not None]

    # -- внутреннее ------------------------------------------------------------

    def _recount(self, card_id: str) -> None:
        ratings = self._journal.entries.get(card_id, {})
        count = len(ratings)
        average = sum(r.value for r in ratings.values()) / count if count else 0.0
        self._journal.summaries[card_id] = CardRating(average=average, count=count)

    # -- сериализуемый снимок журнала (для адаптеров/CLI, не для движка) --------

    def to_records(self) -> list[dict[str, Any]]:
        """Снимок журнала: список plain-dict (в файл/транспорт)."""
        return [
            {"card_id": cid, "user_id": r.user_id, "value": r.value, "text": r.text}
            for cid, per_card in self._journal.entries.items()
            for r in per_card.values()
        ]

    @classmethod
    def from_records(cls, records: list[dict[str, Any]]) -> RatingEngine:
        """Восстановить движок из снимка to_records()."""
        engine = cls()
        for record in records:
            engine.rate(
                record["user_id"],
                record["card_id"],
                record["value"],
                text=record.get("text"),
            )
        return engine


__all__ = [
    "MAX_VALUE",
    "MIN_VALUE",
    "NEUTRAL_VALUE",
    "REVIEW_MIN_COUNT",
    "AlreadyRated",
    "BadRatingValue",
    "CardRating",
    "Rating",
    "RatingEngine",
    "RatingError",
    "RatingJournal",
    "is_for_review",
]
