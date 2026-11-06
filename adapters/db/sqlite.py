"""SqliteCardSource — реализация протокола CardSource поверх sqlite3 (Task 3).

Контракт (engine.interfaces.CardSource) + ruling контроллера:
- fetch_candidates(pair, policy) -> list[Card]: кандидаты с фильтрацией доступа:
  public-карточки отдаются только в статусе pool (draft/retired адаптер не отдаёт),
  private-карточки — только паре автора (draft/retired её автору видит; финальный
  отсев по статусу, анти-повтор и спонсорская капа — движок);
- схема создаётся адаптером при первом подключении; путь к БД — аргумент конструктора.
- pairs_history: персистентный лог доставки — какая карточка уходила паре
  (движок выборку никогда не читает из этой таблицы: PairState он держит
  в памяти и сам передаёт историю правилам выбора).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from engine.interfaces import CardSource  # noqa: F401  (протокол pinned)
from engine.models import (
    Card,
    CardStatus,
    CardVisibility,
    Pair,
    SourceKind,
)
from engine.policy import DeliveryPolicy

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cards (
    id            TEXT PRIMARY KEY,
    theme         TEXT NOT NULL,
    text          TEXT NOT NULL,
    source        TEXT NOT NULL CHECK (source IN ('seed', 'ugc')),
    author_id     TEXT,
    visibility    TEXT NOT NULL CHECK (visibility IN ('private', 'public')),
    status        TEXT NOT NULL CHECK (status IN ('draft', 'pool', 'retired')),
    image_ref     TEXT,
    is_sponsored  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS pairs_history (
    pair_id      TEXT NOT NULL,
    card_id      TEXT NOT NULL,
    card_text    TEXT NOT NULL,
    issued_at    TEXT NOT NULL,
    completed_at TEXT,
    PRIMARY KEY (pair_id, card_id, issued_at)
);

CREATE INDEX IF NOT EXISTS idx_pairs_history_pair ON pairs_history (pair_id);
"""

_SEL_CARDS = "SELECT id, theme, text, source, author_id, visibility, status, image_ref, is_sponsored FROM cards"


def _row_to_card(row: tuple) -> Card:
    (cid, theme, text, source, author_id, visibility, status, image_ref, is_sponsored) = row
    return Card(
        id=cid,
        theme=theme,
        text=text,
        source=SourceKind(source),
        author_id=author_id,
        visibility=CardVisibility(visibility),
        status=CardStatus(status),
        image_ref=image_ref,
        is_sponsored=bool(is_sponsored),
    )


class SqliteCardSource:
    """CardSource поверх файла/пути SQLite. Схему создаёт сам, in-memory допустим."""

    def __init__(self, db_path: str | Path = ":memory:") -> None:
        self.db_path = str(db_path)
        # check_same_thread=False: соединение создаёт и использует один владелец,
        # но жизнь объекта может пересекать потоки (бот/запросы в разных потоках).
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # -- CardSource ----------------------------------------------------------

    def fetch_candidates(self, pair: Pair, policy: DeliveryPolicy) -> list[Card]:
        """Кандидаты для выдачи pair с фильтрацией доступа:

        - public: только status=pool (draft/retired адаптер наружу не отдаёт);
        - private: только паре автора (author_id — один из участников пары);
          автор видит свою карточку в любом статусе, но выдавать её движку
          решать самому (pool/анти-повтор — его работа).
        """
        pair_user_ids = {user.id for user in pair.users}
        rows = self._conn.execute(_SEL_CARDS).fetchall()
        cards = [_row_to_card(r) for r in rows]
        return [
            card
            for card in cards
            if (
                card.visibility == CardVisibility.PUBLIC
                and card.status == CardStatus.POOL
            )
            or (
                card.visibility == CardVisibility.PRIVATE
                and card.author_id in pair_user_ids
            )
        ]

    # -- запись карточек (для сид-загрузки и UGC из Task 5) ------------------

    def add_card(self, card: Card) -> None:
        """Положить/заменить карточку в хранилище."""
        self._conn.execute(
            "INSERT OR REPLACE INTO cards"
            " (id, theme, text, source, author_id, visibility, status, image_ref, is_sponsored)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                card.id,
                card.theme,
                card.text,
                card.source.value,
                card.author_id,
                card.visibility.value,
                card.status.value,
                card.image_ref,
                int(card.is_sponsored),
            ),
        )
        self._conn.commit()

    # -- пары: персистентный учёт показов ------------------------------------

    def record_issue(self, pair: Pair, card: Card, now: datetime | None = None) -> None:
        """Записать показ карточки паре (pairs_history.issued_at) — лог доставки для
        бота/отчётов; движок при выборе карточек эту таблицу не читает."""
        issued_at = (now or datetime.now()).isoformat(timespec="seconds")
        self._conn.execute(
            "INSERT OR IGNORE INTO pairs_history (pair_id, card_id, card_text, issued_at)"
            " VALUES (?, ?, ?, ?)",
            (pair.id, card.id, card.text, issued_at),
        )
        self._conn.commit()

    def mark_completed(self, pair: Pair, card: Card | str, now: datetime | None = None) -> None:
        """Отметить завершение: последний незакрытый показ этой карточки паре."""
        card_id = card.id if isinstance(card, Card) else card
        completed_at = (now or datetime.now()).isoformat(timespec="seconds")
        self._conn.execute(
            "UPDATE pairs_history SET completed_at = ?"
            " WHERE pair_id = ? AND card_id = ? AND completed_at IS NULL",
            (completed_at, pair.id, card_id),
        )
        self._conn.commit()

    def history_texts(self, pair: Pair) -> list[str]:
        """Тексты, показывавшиеся паре (в порядке выдачи) — персистентная история."""
        rows = self._conn.execute(
            "SELECT card_text FROM pairs_history WHERE pair_id = ? ORDER BY issued_at",
            (pair.id,),
        )
        return [r[0] for r in rows]
