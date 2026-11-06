"""Тесты UGC: антифарм голосов, порог автопромоушена, жалобы, premium-гранты."""

from datetime import datetime, timedelta

import pytest

from engine.engine import Engine, NoCardAvailable
from engine.models import Card, CardStatus, CardVisibility, Pair, SourceKind, User
from engine.policy import is_premium_active
from engine.ugc import (
    PROMOTION_LIKES,
    RETIRE_REPORTS,
    AlreadyVoted,
    CardNotIssued,
    NotVotable,
    UgcEngine,
    VoteKind,
    grant_premium_day,
)


def make_user(uid: str) -> User:
    return User(id=uid, username=uid)


def make_pair(pid: str, *uids: str) -> Pair:
    return Pair(id=pid, user_a=make_user(uids[0]), user_b=make_user(uids[1]))


def make_seed(cid: str, text: str) -> Card:
    return Card(id=cid, theme="t", text=text, source=SourceKind.SEED)


class FakeSource:
    """CardSource-заглушка: возвращает фиксированную колоду."""

    def __init__(self, cards: list[Card]) -> None:
        self.cards = cards

    def fetch_candidates(self, pair, policy):
        return list(self.cards)


class FirstRule:
    """RulePlugin-заглушка: берёт первого кандидата."""

    def select(self, candidates, pair_history, policy):
        return candidates[0]


def build_ten_voters(ugc: UgcEngine, card: Card) -> list[User]:
    """Пять пар получают карточку → 10 голосующих-получателей; также их пары."""
    voters: list[User] = []
    for i in range(5):
        pair = make_pair(f"p{i}", f"r{i}a", f"r{i}b")
        ugc.mark_issued(pair, card)
        voters.extend(pair.users)
    return voters


# -- антифарм -----------------------------------------------------------------


def test_non_receiver_like_rejected():
    ugc = UgcEngine()
    author = make_user("a1")
    card = ugc.create_card(author, "т", "Текст", visibility=CardVisibility.PUBLIC)
    outsider = make_user("outsider")
    ugc.mark_issued(make_pair("p1", "r1", "r2"), card)

    with pytest.raises(CardNotIssued):
        ugc.like(outsider, card)
    with pytest.raises(CardNotIssued):
        ugc.dislike(outsider, card)
    assert ugc.state.like_count(card.id) == 0


def test_each_pair_member_votes_once():
    ugc = UgcEngine()
    author = make_user("a1")
    card = ugc.create_card(author, "т", "Текст", visibility=CardVisibility.PUBLIC)
    pair = make_pair("p1", "r1", "r2")
    ugc.mark_issued(pair, card)

    ugc.like(pair.user_a, card)
    with pytest.raises(AlreadyVoted):
        ugc.like(pair.user_a, card)
    with pytest.raises(AlreadyVoted):
        ugc.dislike(pair.user_a, card)  # голос уже потрачен на лайк

    ugc.like(pair.user_b, card)
    assert ugc.state.like_count(card.id) == 2


def test_votes_counted_per_receiver_not_per_pair():
    ugc = UgcEngine()
    card = ugc.create_card(make_user("a1"), "т", "Текст", visibility=CardVisibility.PUBLIC)
    ugc.mark_issued(make_pair("p1", "r1", "r2"), card)
    ugc.mark_issued(make_pair("p2", "r3", "r4"), card)

    for uid in ("r1", "r2", "r3", "r4"):
        ugc.like(make_user(uid), card)
    assert ugc.state.like_count(card.id) == 4


# -- автопромоушен 10/0 ---------------------------------------------------------


def test_ten_likes_zero_dislikes_promotes_and_grants_premium_day():
    ugc = UgcEngine()
    author = make_user("a1")
    card = ugc.create_card(author, "т", "Текст", visibility=CardVisibility.PUBLIC)
    voters = build_ten_voters(ugc, card)

    for voter in voters[: PROMOTION_LIKES - 1]:
        ugc.like(voter, card)
    assert card.status is CardStatus.DRAFT  # 9 лайков мало
    assert author.is_premium is False

    ugc.like(voters[-1], card)  # десятый лайк, дизлайков нет
    assert card.status is CardStatus.POOL
    assert author.is_premium is True
    assert is_premium_active(author)


def test_nine_likes_not_promoted():
    ugc = UgcEngine()
    author = make_user("a1")
    card = ugc.create_card(author, "т", "Текст", visibility=CardVisibility.PUBLIC)
    voters = build_ten_voters(ugc, card)

    for voter in voters[:9]:
        ugc.like(voter, card)
    assert card.status is CardStatus.DRAFT
    assert author.is_premium is False


def test_single_dislike_blocks_promotion():
    ugc = UgcEngine()
    author = make_user("a1")
    card = ugc.create_card(author, "т", "Текст", visibility=CardVisibility.PUBLIC)
    ugc.mark_issued(make_pair("p0", "d1", "x0"), card)  # первый голос — дизлайк
    ugc.dislike(make_user("d1"), card)

    for voter in build_ten_voters(ugc, card):
        ugc.like(voter, card)

    assert ugc.state.like_count(card.id) == 10
    assert ugc.state.dislike_count(card.id) == 1
    assert card.status is CardStatus.DRAFT
    assert author.is_premium is False


def test_promoted_card_is_issued_by_engine():
    ugc = UgcEngine()
    card = ugc.create_card(make_user("a1"), "т", "Текст", visibility=CardVisibility.PUBLIC)
    for voter in build_ten_voters(ugc, card):
        ugc.like(voter, card)
    assert card.status is CardStatus.POOL

    engine = Engine(FakeSource([make_seed("s1", "Сид"), card]), FirstRule())
    pair = make_pair("pd", "d1", "d2")
    assert engine.issue_card(pair).id == card.id  # pool имеет приоритет перед seed


# -- private не входит в автопромоушен -------------------------------------------


def test_private_card_never_promoted():
    ugc = UgcEngine()
    author = make_user("a1")
    card = ugc.create_card(author, "т", "Текст")  # default visibility=private

    for voter in build_ten_voters(ugc, card):
        ugc.like(voter, card)

    assert card.status is CardStatus.DRAFT
    assert author.is_premium is False


def test_default_card_is_private_draft():
    ugc = UgcEngine()
    card = ugc.create_card(make_user("a1"), "т", "Текст")
    assert card.source is SourceKind.UGC
    assert card.visibility is CardVisibility.PRIVATE
    assert card.status is CardStatus.DRAFT
    assert card.author_id == "a1"


# -- ретир по жалобам --------------------------------------------------------------


def test_three_reports_retire_card():
    ugc = UgcEngine()
    card = ugc.create_card(make_user("a1"), "т", "Текст", visibility=CardVisibility.PUBLIC)
    reporters = [make_user(f"rp{i}") for i in range(RETIRE_REPORTS)]

    ugc.report(reporters[0], card)
    assert card.status is CardStatus.DRAFT
    ugc.report(reporters[1], card)
    assert card.status is CardStatus.DRAFT
    ugc.report(reporters[2], card)
    assert card.status is CardStatus.RETIRED


def test_retirement_is_logged(caplog):
    ugc = UgcEngine()
    card = ugc.create_card(make_user("a1"), "т", "Текст")
    for i in range(RETIRE_REPORTS):
        ugc.report(make_user(f"rp{i}"), card)

    messages = [r.getMessage() for r in caplog.records]
    assert any("retired" in m and card.id in m for m in messages), messages


def test_retired_card_not_issued_by_engine():
    """Retired-карточка в колоде адаптера не отдаётся движком выдачи."""
    ugc = UgcEngine()
    card = ugc.create_card(make_user("a1"), "т", "Текст", visibility=CardVisibility.PUBLIC)
    for i in range(RETIRE_REPORTS):
        ugc.report(make_user(f"rp{i}"), card)
    assert card.status is CardStatus.RETIRED

    engine = Engine(FakeSource([make_seed("s1", "Сид"), card]), FirstRule())
    pair = make_pair("p1", "r1", "r2")
    assert engine.issue_card(pair).id == "s1"
    with pytest.raises(NoCardAvailable):
        engine.issue_card(pair)


def test_retired_card_not_votable():
    ugc = UgcEngine()
    card = ugc.create_card(make_user("a1"), "т", "Текст")
    pair = make_pair("p1", "r1", "r2")
    ugc.mark_issued(pair, card)
    for i in range(RETIRE_REPORTS):
        ugc.report(make_user(f"rp{i}"), card)

    with pytest.raises(NotVotable):
        ugc.like(pair.user_a, card)


def test_unique_reporters_only():
    ugc = UgcEngine()
    card = ugc.create_card(make_user("a1"), "т", "Текст")
    spammer = make_user("sp")
    for _ in range(5):
        ugc.report(spammer, card)
    assert card.status is not CardStatus.RETIRED
    assert ugc.state.report_count(card.id) == 1


# -- grant_premium_day --------------------------------------------------------------


def test_grant_premium_day_accumulates():
    user = make_user("u")
    now = datetime(2026, 11, 6, 12, 0, 0)
    first = grant_premium_day(user, now=now)
    assert first == now + timedelta(days=1)
    second = grant_premium_day(user, now=now)
    assert second == now + timedelta(days=2), "дни накапливаются"


def test_grant_extends_future_premium():
    user = make_user("u")
    now = datetime(2026, 11, 6, 12, 0, 0)
    user.is_premium = True
    user.premium_until = now + timedelta(days=3)
    grant_premium_day(user, now=now)
    assert user.premium_until == now + timedelta(days=4)


def test_grant_after_expiry_counts_from_now():
    user = make_user("u")
    user.is_premium = True
    user.premium_until = datetime(2026, 11, 1, 0, 0, 0)
    now = datetime(2026, 11, 6, 12, 0, 0)
    grant_premium_day(user, now=now)
    assert user.premium_until == now + timedelta(days=1)
    assert is_premium_active(user, now=now)


def test_promotion_grants_accumulate_per_card():
    ugc = UgcEngine()
    author = make_user("a1")
    for i in range(2):
        card = ugc.create_card(author, "т", f"Текст {i}", visibility=CardVisibility.PUBLIC)
        for voter in build_ten_voters(ugc, card):
            ugc.like(voter, card)

    assert author.is_premium is True
    remaining = author.premium_until - datetime.now()
    assert timedelta(days=1) < remaining <= timedelta(days=2, minutes=5), (
        "два промоушена должны дать два накопительных дня"
    )
