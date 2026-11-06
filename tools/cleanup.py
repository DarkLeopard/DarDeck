"""Ревью-чистка карточек поверх SQLite-адаптера (controller ruling).

Запуск: docker compose run --rm app python -m tools.cleanup [--db PATH] [--confirm]

- карточки читаются из той же таблицы cards, что и у SqliteCardSource
  (минимальное чтение, без нового SQL-модуля — переиспользуем его же хелперы);
- средние считаются по журналу engine.ratings (СВОЙ журнал, состояние
  ugc-votes не дублируется; снимок журнала — список records JSON);
- ревью-лист: среднее <0 при >=5 оценках (engine.ratings.is_for_review);
- по умолчанию только печать списка; удаление (retire: status -> retired,
  retired не выдаётся движком) — только с --confirm, запись через
  SqliteCardSource.add_card.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Sequence

from adapters.db.sqlite import (
    SqliteCardSource,
    _row_to_card,
    _SEL_CARDS,  # noqa: F401
)
from engine.models import Card, CardStatus
from engine.ratings import (
    CardRating,
    RatingEngine,
    is_for_review,
)

DB_PATH_ENV = "DARDECK_DB_PATH"
RATINGS_ENV = "DARDECK_RATINGS_SNAPSHOT"


def load_cards(source: SqliteCardSource) -> list[Card]:
    """Минимальное чтение той же таблицы cards (все статусы, без фильтра доступа)."""
    rows = source._conn.execute(_SEL_CARDS).fetchall()
    return [_row_to_card(row) for row in rows]


def find_for_review(cards: Sequence[Card], ratings: RatingEngine) -> list[Card]:
    """Ревью-лист: карточки со средним <0 при >=5 оценках."""
    picked: list[Card] = []
    for card in cards:
        card_rating: CardRating = ratings.summary(card.id)
        if is_for_review(card_rating.average, card_rating.count):
            picked.append(card)
    return picked


def retire(source: SqliteCardSource, cards: Sequence[Card], confirm: bool) -> list[Card]:
    """Retire карточек: статус -> retired (движок их больше не выдаёт).

    Без confirm ничего не пишет — ревью-лист только печатается.
    """
    if not confirm:
        return []
    for card in cards:
        card.status = CardStatus.RETIRED
        source.add_card(card)
    return list(cards)


def run(
    db_path: str | Path,
    ratings: RatingEngine | None = None,
    confirm: bool = False,
) -> tuple[list[Card], list[Card]]:
    """Собрать ревью-лист из SQLite-БД (и, с confirm, ретирнуть).

    Возвращает (ревью-лист, ретирнутые); ретирнутые без --confirm пусты.
    """
    source = SqliteCardSource(db_path)
    try:
        cards = load_cards(source)
        review = find_for_review(cards, ratings if ratings is not None else RatingEngine())
        retired = retire(source, review, confirm)
        return review, retired
    finally:
        source.close()


def format_card_row(card: Card, ratings: RatingEngine) -> str:
    """Строка ревью-листа: карточка + её среднее/количество + тексты-отзывы."""
    card_rating = ratings.summary(card.id)
    line = (
        f"{card.id} [{card.visibility.value}] theme={card.theme!r}"
        f" avg={card_rating.average:.2f} count={card_rating.count} :: {card.text!r}"
    )
    lines = [line]
    lines.extend(
        f"    отзыв ({r.user_id}: {r.value:+d}): {r.text}"
        for r in ratings.reviews(card.id)
        if r.text is not None
    )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI: собирает ревью-лист, печатает; с --confirm ретирит."""
    parser = argparse.ArgumentParser(
        prog="python -m tools.cleanup",
        description="Ревью-чистка карточек: среднее <0 при >=5 оценках.",
    )
    parser.add_argument(
        "--db",
        default=os.environ.get(DB_PATH_ENV) or "dardeck.db",
        help=f"путь к SQLite-БД (env {DB_PATH_ENV})",
    )
    parser.add_argument(
        "--ratings",
        default=os.environ.get(RATINGS_ENV),
        help=f"JSON-снимок журнала оценок (env {RATINGS_ENV})",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="выполнить retire найденных карточек (без него — только печать)",
    )
    args = parser.parse_args(argv)

    ratings = RatingEngine()
    if args.ratings:
        with open(args.ratings, encoding="utf-8") as fh:
            ratings = RatingEngine.from_records(json.load(fh))

    review, retired = run(args.db, ratings=ratings, confirm=args.confirm)

    if not review:
        print("Ревью-лист пуст: карточек со средним <0 при >=5 оценках нет.")
        return 0

    print(f"Ревью-лист ({len(review)}):")
    for card in review:
        print(format_card_row(card, ratings))
    if args.confirm:
        print(f"Retired по --confirm: {len(retired)} карточка(ок).")
    else:
        print("Только печать: для ретира добавьте --confirm.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
