"""Тесты ratings + cleanup (Task 6): оценки/отзывы, ревью-лист, ретир по --confirm.

- ratings: оценка +1/0/-1 + текст-отзыв; по карточке хранятся среднее и количество;
- ревью-лист cleanup'а: среднее <0 при >=5 оценках; 4 оценивания — недостаточно;
- --confirm ретирит карточку в SQLite (status -> retired), дальше она не выдаётся;
- без --confirm ничего не меняется (только печать).
"""

import json

import pytest

from adapters.db.sqlite import SqliteCardSource
from adapters.rules.rotation_rule import RotationRule
from engine.engine import Engine, NoCardAvailable
from engine.models import Card, CardStatus, CardVisibility, Pair, SourceKind, User
from engine.ratings import (
    REVIEW_MIN_COUNT,
    AlreadyRated,
    BadRatingValue,
    Rating,
    RatingEngine,
    is_for_review,
)
from tools.cleanup import find_for_review, load_cards, main, run


def make_user(uid: str) -> User:
    return User(id=uid, username=uid)


def make_pair(pid: str = "p1", users: tuple[str, str] = ("a", "b")) -> Pair:
    return Pair(id=pid, user_a=make_user(users[0]), user_b=make_user(users[1]))


def make_pool_card(cid: str, text: str) -> Card:
    return Card(
        id=cid,
        theme="т",
        text=text,
        source=SourceKind.UGC,
        visibility=CardVisibility.PUBLIC,
        status=CardStatus.POOL,
        author_id="a1",
    )


# -- ratings: оценки +1/0/-1, среднее и количество --------------------------------


def test_rate_values_and_stored_average_count():
    ratings = RatingEngine()
    card = make_pool_card("c1", "Текст")

    ratings.rate(make_user("u1"), card, 1)
    ratings.rate(make_user("u2"), card, 0)
    ratings.rate(make_user("u3"), card, -1)

    card_rating = ratings.summary(card.id)
    assert card_rating.count == 3
    assert card_rating.average == pytest.approx(0.0)
    assert ratings.count(card.id) == 3
    assert ratings.average(card) == 0.0


def test_average_negative_and_no_ratings_defaults():
    ratings = RatingEngine()
    card = make_pool_card("c1", "Текст")

    assert ratings.summary(card.id).count == 0
    assert ratings.summary(card.id).average == 0.0
    for uid in ("u1", "u2", "u3", "u4"):
        ratings.rate(uid, card, -1)
    assert ratings.average(card.id) == pytest.approx(-1.0)
    assert ratings.count(card) == 4


def test_review_text_optional_and_returned():
    ratings = RatingEngine()
    card = make_pool_card("c1", "Текст")

    rating = ratings.rate("u1", card, -1, text="Слишком грустно")
    assert rating == Rating(user_id="u1", value=-1, text="Слишком грустно")

    ratings.rate("u2", card, 1)  # без текста
    assert ratings.review_texts(card.id) == ["Слишком грустно"]
    assert len(ratings.reviews(card.id)) == 2  # все оценки, текст опционален


def test_bad_value_rejected():
    ratings = RatingEngine()
    card = make_pool_card("c1", "Текст")

    for bad in (2, -2, "like"):
        with pytest.raises(BadRatingValue):
            ratings.rate("u1", card, bad)
    assert ratings.count(card.id) == 0


def test_second_rating_by_same_user_rejected():
    ratings = RatingEngine()
    card = make_pool_card("c1", "Текст")

    ratings.rate(make_user("u1"), card, 1)
    with pytest.raises(AlreadyRated):
        ratings.rate(make_user("u1"), card, -1)
    assert ratings.count(card.id) == 1
    assert ratings.reviews(card.id)[0].value == 1


def test_ratings_do_not_touch_ugc_state():
    """Интеграция: журнал ratings свой, состояние ugc (votes) не дублируется."""
    from engine.ugc import UgcEngine

    ugc = UgcEngine()
    card = ugc.create_card(make_user("a1"), "т", "Текст", visibility=CardVisibility.PUBLIC)
    ratings = RatingEngine()
    ratings.rate(make_user("u1"), card, -1)

    assert ugc.state.votes == {}
    assert ratings.count(card.id) == 1


# -- сериализуемый снимок -----------------------------------------------------------


def test_records_round_trip():
    ratings = RatingEngine()
    for uid in ("u1", "u2"):
        ratings.rate(uid, "c1", -1, text=f"отзыв {uid}")

    restored = RatingEngine.from_records(json.loads(json.dumps(ratings.to_records())))
    assert restored.summary("c1") == ratings.summary("c1")
    assert restored.review_texts("c1") == ratings.review_texts("c1")


# -- порог ревью-листа: <0 при >=5 ---------------------------------------------------


def test_is_for_review_threshold():
    assert is_for_review(average=-0.2, count=5)
    assert not is_for_review(average=-1.0, count=REVIEW_MIN_COUNT - 1)
    assert not is_for_review(average=0.0, count=5)
    assert not is_for_review(average=0.5, count=10)
    assert not is_for_review(average=1.0, count=0)


def test_find_for_review_over_cards():
    rated = make_pool_card("bad", "Плохая")
    clean = make_pool_card("ok", "Нормальная")
    ratings = RatingEngine()
    for i in range(5):
        ratings.rate(f"u{i}", rated, -1)
    assert find_for_review([rated, clean], ratings) == [rated]


def test_review_list_five_bad_ratings_in(tmp_path):
    card = make_pool_card("bad", "Плохая карточка")
    good = make_pool_card("good", "Хорошая карточка")
    db = tmp_path / "deck.sqlite"
    src = SqliteCardSource(db)
    src.add_card(card)
    src.add_card(good)
    src.close()

    ratings = RatingEngine()
    for uid in ("u1", "u2", "u3", "u4", "u5"):
        ratings.rate(make_user(uid), card, -1)
    for uid in ("u1", "u2", "u3", "u4"):
        ratings.rate(make_user(uid), good, -1)  # 4 оценки — рано

    review, retired = run(db, ratings=ratings, confirm=False)
    assert [c.id for c in review] == ["bad"]  # 5 оценок avg -1 → в списке; 4 → нет
    assert retired == []  # без --confirm ничего не меняется


def test_review_list_boundary_avg_zero_not_in(tmp_path):
    card = make_pool_card("neutral", "Спорная карточка")
    db = tmp_path / "deck.sqlite"
    src = SqliteCardSource(db)
    src.add_card(card)
    src.close()

    ratings = RatingEngine()
    ratings.rate("u1", card.id, -1)
    ratings.rate("u2", card.id, -1)
    ratings.rate("u3", card.id, 1)
    ratings.rate("u4", card.id, 1)
    ratings.rate("u5", card.id, 0)  # 5 оценок, среднее ровно 0 — не <0

    review, _ = run(db, ratings=ratings)
    assert review == []


# -- ретир по --confirm и невыдаемость ------------------------------------------------


def make_db_with_rated_card(tmp_path, card_id: str = "bad") -> tuple[str, Card, RatingEngine]:
    """Файловая БД с одной pool-карточкой, оценённой 5×(-1)."""
    card = make_pool_card(card_id, "Плохая карточка")
    db = tmp_path / "deck.sqlite"
    src = SqliteCardSource(db)
    src.add_card(card)
    src.close()
    ratings = RatingEngine()
    for i in range(5):
        ratings.rate(make_user(f"u{i}"), card, -1)
    return str(db), card, ratings


def get_db_status(db: str, card_id: str) -> str:
    src = SqliteCardSource(db)
    try:
        rows = load_cards(src)
    finally:
        src.close()
    return next(c.status for c in rows if c.id == card_id)


def test_confirm_retires_card_and_it_is_not_issued(tmp_path):
    db, card, ratings = make_db_with_rated_card(tmp_path)

    review, retired = run(db, ratings=ratings, confirm=True)

    assert [c.id for c in review] == [card.id]
    assert [c.id for c in retired] == [card.id]
    assert get_db_status(db, card.id) == CardStatus.RETIRED

    # thereafter card is not issued: движок над той же БД уже не отдаёт её
    src = SqliteCardSource(db)
    engine = Engine(src, RotationRule())
    try:
        with pytest.raises(NoCardAvailable):
            engine.issue_card(make_pair())
    finally:
        src.close()


def test_no_confirm_changes_nothing(tmp_path):
    db, card, ratings = make_db_with_rated_card(tmp_path)
    review, retired = run(db, ratings=ratings, confirm=False)

    assert [c.id for c in review] == [card.id]
    assert retired == []
    assert get_db_status(db, card.id) == CardStatus.POOL  # только печать


# -- CLI ------------------------------------------------------------------------------


def test_cli_prints_list_without_touching_db(tmp_path, capsys):
    db, card, ratings = make_db_with_rated_card(tmp_path)
    snapshot = tmp_path / "ratings.json"
    snapshot.write_text(json.dumps(ratings.to_records()), encoding="utf-8")

    exit_code = main(["--db", db, "--ratings", str(snapshot)])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert card.id in out
    assert "Только печать" in out
    assert get_db_status(db, card.id) == CardStatus.POOL


def test_cli_confirm_retires(tmp_path, capsys):
    db, card, ratings = make_db_with_rated_card(tmp_path)
    snapshot = tmp_path / "ratings.json"
    snapshot.write_text(json.dumps(ratings.to_records()), encoding="utf-8")

    exit_code = main(["--db", db, "--ratings", str(snapshot), "--confirm"])

    assert exit_code == 0
    assert "Retired" in capsys.readouterr().out
    assert get_db_status(db, card.id) == CardStatus.RETIRED


def test_cli_empty_review_list(tmp_path, capsys):
    db = tmp_path / "deck.sqlite"
    src = SqliteCardSource(db)
    src.add_card(make_pool_card("ok", "Нормальная"))
    src.close()

    assert main(["--db", str(db)]) == 0
    assert "Ревью-лист пуст" in capsys.readouterr().out
