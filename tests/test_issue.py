"""Тесты issue_card: anti-repeat по тексту, приоритет pool->seed, sponsored-cap 1/7, учёт."""

from collections import deque
from datetime import timedelta

import pytest

from engine.engine import Engine, NoCardAvailable, PairState, SPONSORED_CYCLE
from engine.models import Card, CardStatus, CardVisibility, Pair, SourceKind, User
from engine.policy import DeliveryPolicy


def make_user(uid: str) -> User:
    return User(id=uid, username=uid)


def make_pair(pid: str = "p1") -> Pair:
    return Pair(id=pid, user_a=make_user("a"), user_b=make_user("b"))


def make_card(cid: str, text: str, *, source: SourceKind = SourceKind.SEED,
              status: CardStatus = CardStatus.POOL, is_sponsored: bool = False) -> Card:
    return Card(id=cid, theme="t", text=text, source=source, status=status,
                is_sponsored=is_sponsored)


class FakeSource:
    """CardSource-заглушка: фиксирует вызовы, возвращает заданную колоду."""

    def __init__(self, cards: list[Card]) -> None:
        self.cards = cards
        self.calls: list[tuple[Pair, DeliveryPolicy]] = []

    def fetch_candidates(self, pair, policy):
        self.calls.append((pair, policy))
        return list(self.cards)


class FirstRule:
    """RulePlugin-заглушка: берёт первого кандидата, запоминает аргументы."""

    def __init__(self) -> None:
        self.last_candidates: list[Card] | None = None
        self.last_history: tuple[str, ...] | None = None
        self.last_policy: DeliveryPolicy | None = None

    def select(self, candidates, pair_history, policy):
        self.last_candidates = list(candidates)
        self.last_history = tuple(pair_history)
        self.last_policy = policy
        return candidates[0]

# -- базовая выдача ----------------------------------------------------------


def test_issue_returns_card_and_records_issued():
    deck = [make_card("c1", "Текст 1")]
    engine = Engine(FakeSource(deck), FirstRule())
    pair = make_pair()
    card = engine.issue_card(pair)
    assert card.id == "c1"
    state = engine.pair_state(pair)
    assert state.issued == {"c1"}
    assert state.history == ["Текст 1"]


def test_engine_passes_pair_and_policy_to_source_and_rule():
    deck = [make_card("c1", "x")]
    source = FakeSource(deck)
    rule = FirstRule()
    policy = DeliveryPolicy(interval=timedelta(hours=2), cards_per_cycle=2)
    engine = Engine(source, rule, policy=policy)
    pair = make_pair()
    engine.issue_card(pair)
    assert source.calls == [(pair, policy)]
    assert rule.last_policy is policy
    assert rule.last_candidates[0].id == "c1"
    assert rule.last_history == ()


# -- anti-repeat по тексту ---------------------------------------------------


class LastRule(FirstRule):
    def select(self, candidates, pair_history, policy):
        super().select(candidates, pair_history, policy)
        return candidates[-1]


def test_anti_repeat_no_same_text_twice():
    deck = [
        make_card("c1", "Текст 1"),
        make_card("c2", "Текст 2"),
        make_card("c3", "Текст 3"),
        make_card("dup-c", "Текст 3"),  # дубликат текста не должен выдаваться снова
    ]
    engine = Engine(FakeSource(deck), LastRule())
    pair = make_pair()
    texts = [engine.issue_card(pair).text for _ in range(3)]
    assert len(texts) == len(set(texts)) == 3
    with pytest.raises(NoCardAvailable):
        engine.issue_card(pair)


def test_repeat_after_completion_still_blocked_by_text_dedup():
    deck = [make_card("c1", "Текст 1"), make_card("c2", "Текст 2")]
    engine = Engine(FakeSource(deck), FirstRule())
    pair = make_pair()
    engine.issue_card(pair)
    engine.mark_completed(pair, "c1")
    assert engine.pair_state(pair).completed == {"c1"}
    assert engine.issue_card(pair).text == "Текст 2"
    engine.mark_completed(pair, "c2")
    with pytest.raises(NoCardAvailable):
        engine.issue_card(pair)


# -- приоритет pool -> seed ---------------------------------------------------


def test_pool_cards_take_priority_over_seed():
    deck = [
        make_card("seed1", "Сид", source=SourceKind.SEED),
        make_card("ugc1", "ЮГЦ", source=SourceKind.UGC),
    ]
    engine = Engine(FakeSource(deck), FirstRule())
    pair = make_pair()
    assert engine.issue_card(pair).id == "ugc1"
    assert engine.issue_card(pair).id == "seed1"


def test_draft_and_retired_ugc_are_not_issued():
    deck = [
        make_card("seed1", "Сид", source=SourceKind.SEED),
        make_card("draft", "Черновик", source=SourceKind.UGC, status=CardStatus.DRAFT),
        make_card("retired", "Отставлена", source=SourceKind.UGC, status=CardStatus.RETIRED),
    ]
    engine = Engine(FakeSource(deck), LastRule())
    pair = make_pair()
    assert engine.issue_card(pair).id == "seed1"
    with pytest.raises(NoCardAvailable):
        engine.issue_card(pair)
    state = engine.pair_state(pair)
    assert "Черновик" not in state.history and "Отставлена" not in state.history


# -- private UGC: паре автора — да (draft включительно), чужим парам — нет -----


def make_ugc(cid: str, text: str, author_id: str | None, status: CardStatus) -> Card:
    """UGC-карточка с автором: private-видимость по умолчанию."""
    return Card(
        id=cid,
        theme="t",
        text=text,
        source=SourceKind.UGC,
        author_id=author_id,
        visibility=CardVisibility.PRIVATE,
        status=status,
    )


def test_author_pair_receives_own_private_card_regardless_of_draft():
    """Решение по private-истории: приватную карточку автора паре автора выдать можно."""
    own_pair = Pair(id="p1", user_a=make_user("a"), user_b=make_user("b"))
    for status in (CardStatus.DRAFT, CardStatus.POOL):
        card = make_ugc("priv", "Личная", "a", status)
        engine = Engine(FakeSource([card]), FirstRule())
        assert engine.issue_card(own_pair).id == "priv"


def test_other_pair_does_not_receive_private_card():
    """Чужим парам private-черновик (авторская исключение) не выдаётся."""
    deck = [
        make_card("seed1", "Сид", source=SourceKind.SEED),
        make_ugc("priv", "Личная", "a", CardStatus.DRAFT),
    ]
    engine = Engine(FakeSource(deck), FirstRule())
    other_pair = Pair(id="p2", user_a=make_user("x"), user_b=make_user("y"))
    assert engine.issue_card(other_pair).id == "seed1"  # только seed
    with pytest.raises(NoCardAvailable):
        engine.issue_card(other_pair)  # чужая private недоступна


def test_retired_private_card_not_issued_even_to_author_pair():
    """Retired снят пост-модерацией: приватная карточка автора больше не выдаётся."""
    card = make_ugc("priv", "Личная", "a", CardStatus.RETIRED)
    engine = Engine(FakeSource([card]), FirstRule())
    own_pair = Pair(id="p1", user_a=make_user("a"), user_b=make_user("b"))
    with pytest.raises(NoCardAvailable):
        engine.issue_card(own_pair)


def test_exhaustion_raises_when_all_shown():
    deck = [make_card("c1", "Текст 1")]
    engine = Engine(FakeSource(deck), FirstRule())
    pair = make_pair()
    engine.issue_card(pair)
    with pytest.raises(NoCardAvailable):
        engine.issue_card(pair)


# -- спонсорские: ≤1 на 7 показов --------------------------------------------


def test_sponsored_cap_never_two_in_seven_shows():
    deck = [make_card(f"c{i}", f"Текст {i}", is_sponsored=(i % 3 == 0)) for i in range(60)]
    engine = Engine(FakeSource(deck), LastRule())
    pair = make_pair()
    flags = [engine.issue_card(pair).is_sponsored for _ in range(40)]
    window = deque(maxlen=SPONSORED_CYCLE)
    for flag in flags:
        window.append(flag)
        assert sum(window) <= 1, "нарушена капа ≤1 спонсорская на 7 показов"


def test_sponsored_can_repeat_after_cycle():
    deck = [
        make_card("s1", "Спонсор 1", is_sponsored=True),
        *[make_card(f"n{i}", f"Обычная {i}") for i in range(SPONSORED_CYCLE)],
        make_card("s2", "Спонсор 2", is_sponsored=True),
    ]
    engine = Engine(FakeSource(deck), FirstRule())
    pair = make_pair()
    assert engine.issue_card(pair).id == "s1"
    ids = [engine.issue_card(pair).id for _ in range(SPONSORED_CYCLE)]
    assert "s2" not in ids
    assert engine.issue_card(pair).id == "s2"


def test_sponsored_blocked_even_if_only_sponsored_left():
    deck = [
        make_card("s1", "Спонсор 1", is_sponsored=True),
        make_card("s2", "Спонсор 2", is_sponsored=True),
    ]
    engine = Engine(FakeSource(deck), FirstRule())
    pair = make_pair()
    assert engine.issue_card(pair).id == "s1"
    with pytest.raises(NoCardAvailable):
        engine.issue_card(pair)


# -- состояние пары изолировано -----------------------------------------------


def test_pair_state_is_isolated_per_pair():
    deck = [make_card("c1", "Текст 1"), make_card("c2", "Текст 2")]
    engine = Engine(FakeSource(deck), FirstRule())
    p1, p2 = make_pair("p1"), make_pair("p2")
    assert engine.issue_card(p1).id == "c1"
    assert engine.issue_card(p2).id == "c1"  # своя история: та же карточка доступна
    assert engine.pair_state(p1).issued == {"c1"}
    assert engine.pair_state(p2).issued == {"c1"}
    assert engine.pair_state(p1) is not engine.pair_state(p2)


def test_pair_state_defaults():
    state = PairState()
    assert state.history == [] and state.issued == set() and state.completed == set()
    assert state.sponsored_window.maxlen == SPONSORED_CYCLE - 1
