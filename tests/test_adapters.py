"""Тесты адаптеров (Task 3): SqliteCardSource, RandomRule, RotationRule.

- SqliteCardSource: схема создаёт адаптер, БД — аргумент конструктора;
  fetch_candidates фильтрует доступ: public — только pool (draft/retired наружу
  не уходят), private — только паре автора (никаких утечек чужих приватных карточек);
  record_issue / mark_completed пишут лог доставки pairs_history.
- RandomRule: детерминированный с сидом; RotationRule: unseen первыми, затем LRU;
- интеграция: Engine + SqliteCardSource + RotationRule без сети.
"""

import random

import pytest

from adapters.db.sqlite import SqliteCardSource
from adapters.rules.random_rule import RandomRule
from adapters.rules.rotation_rule import RotationRule
from engine.engine import Engine, NoCardAvailable
from engine.models import Card, CardStatus, CardVisibility, Pair, SourceKind, User
from engine.policy import DeliveryPolicy


def make_user(uid: str) -> User:
    return User(id=uid, username=uid)


def make_pair(pid: str = "p1", users: tuple[str, str] = ("a", "b")) -> Pair:
    return Pair(id=pid, user_a=make_user(users[0]), user_b=make_user(users[1]))


def make_card(cid: str, text: str, *, source: SourceKind = SourceKind.SEED,
              status: CardStatus = CardStatus.POOL, is_sponsored: bool = False,
              visibility: CardVisibility | None = None,
              author_id: str | None = None) -> Card:
    if visibility is None:
        # seed — публичная колода; UGC по умолчанию приватная карточка автора
        visibility = CardVisibility.PUBLIC if source == SourceKind.SEED else CardVisibility.PRIVATE
    return Card(id=cid, theme="t", text=text, source=source, author_id=author_id,
                visibility=visibility, status=status, is_sponsored=is_sponsored)


def make_source(*cards: Card) -> SqliteCardSource:
    src = SqliteCardSource(":memory:")
    for card in cards:
        src.add_card(card)
    return src


# -- SqliteCardSource ----------------------------------------------------------


def test_schema_created_by_adapter_at_ctor_path(tmp_path):
    db = tmp_path / "deck.sqlite"
    assert not db.exists()
    src = SqliteCardSource(db)  # путь — аргумент конструктора
    src.close()
    assert db.exists()
    tables = {
        row[0]
        for row in SqliteCardSource(db)._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"cards", "pairs_history"} <= tables


def test_fetch_candidates_public_draft_and_retired_not_returned(tmp_path):
    """Ruling: public-карточки в статусах draft/retired адаптер не отдаёт."""
    src = make_source(
        make_card("s1", "Сид"),
        make_card("d1", "Публичный черновик",
                  source=SourceKind.UGC, status=CardStatus.DRAFT,
                  visibility=CardVisibility.PUBLIC),
        make_card("r1", "Ретирнутая",
                  source=SourceKind.UGC, status=CardStatus.RETIRED,
                  visibility=CardVisibility.PUBLIC),
    )
    pair = make_pair()
    got = src.fetch_candidates(pair, DeliveryPolicy())
    assert [c.text for c in got] == ["Сид"]  # в пуле только seed
    src.close()


def test_private_card_not_leaked_to_other_pair(tmp_path):
    """Ruling: private-карточка видна только паре автора — никакой утечки чужим парам."""
    src = make_source(make_card("u1", "Личная", source=SourceKind.UGC, author_id="a"))
    own_pair = make_pair("pown", users=("a", "b"))
    other_pair = make_pair("pother", users=("x", "y"))
    assert [c.id for c in src.fetch_candidates(own_pair, DeliveryPolicy())] == ["u1"]
    assert src.fetch_candidates(other_pair, DeliveryPolicy()) == []  # утечки нет
    src.close()


def test_private_card_of_second_pair_member_visible_to_author_pair(tmp_path):
    """Автор — любой участник пары (user_a или user_b), а не только user_a."""
    src = make_source(
        make_card("ua", "От Эй", source=SourceKind.UGC, author_id="a"),
        make_card("ub", "От Би", source=SourceKind.UGC, author_id="b"),
    )
    own_pair = make_pair("pown", users=("a", "b"))
    other = make_pair("pother", users=("x", "y"))
    got = {c.author_id for c in src.fetch_candidates(own_pair, DeliveryPolicy())}
    assert got == {"a", "b"}
    assert src.fetch_candidates(other, DeliveryPolicy()) == []
    src.close()


def test_private_draft_visible_to_author_pair_in_any_status(tmp_path):
    """Private-карточку адаптер фильтрует только по автору пары, не по статусу;
    финальное решение о выдаче (pool / анти-повтор) — движок."""
    src = make_source(
        make_card("d1", "Черновик автора", source=SourceKind.UGC,
                  status=CardStatus.DRAFT, author_id="a"),
    )
    assert [c.id for c in src.fetch_candidates(make_pair("pown", ("a", "b")), DeliveryPolicy())] == ["d1"]
    assert src.fetch_candidates(make_pair("p other", ("x", "y")), DeliveryPolicy()) == []
    src.close()


def test_unauthored_private_card_visible_to_nobody():
    """Private-карточка без author_id не принадлежит никому — не отдаётся даже «своей» паре."""
    src = make_source(make_card("orphan", "Ничья", source=SourceKind.UGC))
    for pair in (make_pair("pown", ("a", "b")), make_pair("pother", ("x", "y"))):
        assert src.fetch_candidates(pair, DeliveryPolicy()) == []
    src.close()


def test_card_roundtrip_preserves_fields():
    card = make_card("sp1", "Спонсор", source=SourceKind.UGC, is_sponsored=True,
                     visibility=CardVisibility.PUBLIC, author_id="u1")
    src = make_source(card)
    (out,) = src.fetch_candidates(make_pair(), DeliveryPolicy())  # public pool — видна любой паре
    assert out == card
    private = make_card("pv1", "Личная", source=SourceKind.UGC, author_id="a")
    src.add_card(private)
    got = {c.id: c for c in src.fetch_candidates(make_pair("pown", ("a", "b")), DeliveryPolicy())}
    assert got["pv1"] == private
    src.close()


def test_record_issue_and_complete_in_pairs_history():
    src = make_source(make_card("c1", "Текст 1"))
    pair = make_pair("p9")
    card = src.fetch_candidates(pair, DeliveryPolicy())[0]
    src.record_issue(pair, card)
    src.record_issue(pair, card)  # повторный показ не плодит дублей (INSERT OR IGNORE)
    assert src.history_texts(pair) == ["Текст 1"]
    assert len(src._conn.execute("SELECT rowid FROM pairs_history WHERE pair_id='p9'").fetchall()) == 1

    src.mark_completed(pair, "c1")
    (completed_at,) = src._conn.execute(
        "SELECT completed_at FROM pairs_history WHERE pair_id='p9'"
    ).fetchone()
    assert completed_at is not None
    # завершение другой карточки ничего не трогает
    src.mark_completed(pair, "cX")
    (completed_at,) = src._conn.execute(
        "SELECT completed_at FROM pairs_history WHERE pair_id='p9'"
    ).fetchone()
    assert completed_at is not None
    src.close()


def test_history_isolated_per_pair():
    src = make_source(make_card("c1", "Текст 1"))
    card = src.fetch_candidates(make_pair(), DeliveryPolicy())[0]
    src.record_issue(make_pair("pA"), card)
    assert src.history_texts(make_pair("pB")) == []
    src.close()


# -- RandomRule ----------------------------------------------------------------


def test_random_rule_seed_is_deterministic_and_in_candidates():
    cards = [make_card(f"c{i}", f"Текст {i}") for i in range(10)]
    pair, policy = make_pair(), DeliveryPolicy()
    history: tuple[str, ...] = ()
    picks_a = [RandomRule(random.Random(42)).select(cards, history, policy).id for _ in range(20)]
    picks_b = [RandomRule(random.Random(42)).select(cards, history, policy).id for _ in range(20)]
    assert picks_a == picks_b
    ids = {c.id for c in cards}
    assert set(picks_a) <= ids


def test_random_rule_rejects_empty_candidates():
    with pytest.raises(ValueError):
        RandomRule().select([], (), DeliveryPolicy())


# -- RotationRule ---------------------------------------------------------------


def test_rotation_rule_picks_unseen_first_in_order():
    cards = [make_card(f"c{i}", f"Текст {i}") for i in range(3)]
    rule = RotationRule()
    assert rule.select(cards, (), DeliveryPolicy()) is cards[0]
    # "Текст 0" уже показан → первый unseen
    assert rule.select(cards, ("Текст 0",), DeliveryPolicy()) is cards[1]
    assert rule.select(cards, ("Текст 1", "Текст 0"), DeliveryPolicy()) is cards[2]


def test_rotation_rule_all_seen_picks_lru():
    cards = [make_card(f"c{i}", f"Текст {i}") for i in range(3)]
    rule = RotationRule()
    # "Текст 2" показан давнее всех (последний раз на позиции 0) — возвращается первым
    picked = rule.select(cards, ("Текст 2", "Текст 0", "Текст 1"), DeliveryPolicy())
    assert picked.id == "c2"


def test_rotation_rule_rejects_empty_candidates():
    with pytest.raises(ValueError):
        RotationRule().select([], (), DeliveryPolicy())


# -- интеграция: движок на SqliteCardSource --------------------------------------


def test_engine_with_sqlite_source_rotation_no_repeat_and_pool_priority():
    src = make_source(
        make_card("seed1", "Сид"),
        make_card("ugc1", "Пул-ЮГЦ", source=SourceKind.UGC,
                  visibility=CardVisibility.PUBLIC, author_id="u1"),
        make_card("draft", "Черновик", source=SourceKind.UGC, status=CardStatus.DRAFT,
                  visibility=CardVisibility.PUBLIC, author_id="u1"),
    )
    engine = Engine(src, RotationRule())
    pair = make_pair()
    assert engine.issue_card(pair).id == "ugc1"  # pool приоритетнее seed
    assert engine.issue_card(pair).id == "seed1"
    with pytest.raises(NoCardAvailable):
        engine.issue_card(pair)  # draft отфильтрован (адаптер и движок), всё показано
    state = engine.pair_state(pair)
    assert "Черновик" not in state.history
    src.close()


def test_engine_private_card_issued_to_author_pair_only():
    """Приватная карточка автора выдаётся его паре, но не утекает чужим парам."""
    src = make_source(
        make_card("seed1", "Сид"),
        make_card("priv", "Личная", source=SourceKind.UGC, author_id="a"),
    )
    engine = Engine(src, RotationRule())
    own = make_pair("pown", ("a", "b"))
    other = make_pair("pother", ("x", "y"))
    assert engine.issue_card(other).id == "seed1"  # приватная чужой паре не видна
    assert "Личная" not in engine.pair_state(other).history
    assert engine.issue_card(own).id == "priv"  # своей паре — первее seed (pool -> seed)
    src.close()


def test_engine_persistence_survives_source_reconnect(tmp_path):
    """Персистентная история: новый-инстанс адаптера на том же файле видит показы."""
    db = tmp_path / "deck.sqlite"
    src = SqliteCardSource(db)
    src.add_card(make_card("c1", "Текст 1"))
    pair = make_pair("pX")
    card = src.fetch_candidates(pair, DeliveryPolicy())[0]
    src.record_issue(pair, card)
    src.close()

    reconnected = SqliteCardSource(db)
    assert reconnected.history_texts(pair) == ["Текст 1"]
    reconnected.close()
