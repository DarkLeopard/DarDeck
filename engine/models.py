"""Домены движка: User, Pair, Card (спецификация Dardeck/Delight)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class SourceKind(str, Enum):
    """Происхождение карточки: seed-колода или UGC.

    Имя NOT `CardSource` — это имя занято протоколом адаптера в engine.interfaces.
    """

    SEED = "seed"
    UGC = "ugc"


class CardVisibility(str, Enum):
    PRIVATE = "private"
    PUBLIC = "public"


class CardStatus(str, Enum):
    DRAFT = "draft"
    POOL = "pool"
    RETIRED = "retired"


@dataclass
class User:
    id: str
    username: str
    is_premium: bool = False
    premium_until: datetime | None = None


@dataclass
class Pair:
    """Двое пользователей + общий контекст показанных карточек.

    Контекст (история показов) держит движок в in-memory состоянии по pair.id;
    Pair — идентификация пары.
    """

    id: str
    user_a: User
    user_b: User

    @property
    def users(self) -> tuple[User, User]:
        return (self.user_a, self.user_b)


@dataclass
class Card:
    id: str
    theme: str
    text: str
    source: SourceKind
    author_id: str | None = None
    visibility: CardVisibility = CardVisibility.PRIVATE
    status: CardStatus = CardStatus.DRAFT
    image_ref: str | None = None
    is_sponsored: bool = False
