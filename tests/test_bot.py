"""Интеграционные тесты бота поверх движка: MockSender, без сети и токена.

Покрытие Task 4:
- пара юзеров: /pair -> /today (карточка обоим) -> /done -> /rate (лог);
- гейт свободного интервала (DeliveryPolicy: сервер решает, 1/день);
- dry-run fallback: без DARDECK_BOT_TOKEN -> LogSender instead of TelegramSender;
- модуль импортируется без токена и без aiogram на верхнем уровне (subprocess).
"""

import subprocess
import sys
from pathlib import Path

import pytest

from adapters.rules.rotation_rule import RotationRule
from bots.sender import LogSender, MockSender, Sender
from bots.telegram import bot as bot_module
from bots.telegram.bot import (
    AlreadyIssued,
    BotError,
    BotService,
    NoPair,
    NotIssued,
    TelegramSender,
    get_token,
)
from engine.engine import Engine, NoCardAvailable
from engine.models import Card, CardStatus, Pair, SourceKind, User

REPO_ROOT = Path(__file__).resolve().parent.parent


def make_user(uid: str) -> User:
    return User(id=uid, username=uid)


def make_card(cid: str, text: str, **kwargs) -> Card:
    defaults = dict(theme="тема", text=text, source=SourceKind.SEED,
                    status=CardStatus.POOL)
    defaults.update(kwargs)
    return Card(id=cid, **defaults)


class ListSource:
    """CardSource-заглушка: фиксирует колоду (как FakeSource в test_issue)."""

    def __init__(self, cards: list[Card]) -> None:
        self.cards = cards

    def fetch_candidates(self, pair: Pair, policy):
        return list(self.cards)


def make_service(sender: Sender | None = None,
                 cards: list[Card] | None = None) -> BotService:
    deck = cards if cards is not None else [
        make_card("c1", "Спросите партнёра о детстве"),
        make_card("c2", "Свидание на выходные вместе"),
        make_card("c3", "Мечта, о которой вы не рассказывали"),
    ]
    engine = Engine(ListSource(deck), RotationRule())
    return BotService(engine, sender if sender is not None else MockSender())


def pair_up(service: BotService) -> None:
    service.start("u1", "ann")
    service.start("u2", "bob")
    service.pair("u1", "ann")
    service.pair("u2", "bob")


# -- спаривание ---------------------------------------------------------------


def test_pair_two_users_both_sides():
    service = make_service()
    pair_up(service)
    pair = service.pair_for("u1")
    assert pair is not None
    assert pair is service.pair_for("u2")
    assert {u.id for u in pair.users} == {"u1", "u2"}


def test_today_without_pair_raises_no_pair():
    service = make_service()
    with pytest.raises(NoPair):
        service.today("u1")


def test_start_registers_and_describes_steps():
    service = make_service()
    reply = service.start("u1", "ann")
    assert "/pair" in reply and "/today" in reply


# -- интеграция: issue today -> done -> rate ----------------------------------


def test_two_user_pair_flow_today_done_rate():
    """Основной флоу Task 4: пара -> /today (оба получают) -> /done -> /rate."""
    sender = MockSender()
    service = make_service(sender=sender)
    pair_up(service)

    reply = service.today("u1")
    assert "Карточка дня" in reply
    # карточка ушла ОБОИМ юзерам через Sender
    assert len(sender.sent) == 2
    assert {u.id for u, _ in sender.sent} == {"u1", "u2"}
    assert sender.sent[0][1].id == "c1"  # RotationRule берёт первую не показанную

    pair = service.pair_for("u1")
    engine = service._engine  # noqa: SLF001 (тест интеграции: проверяем движок)
    state = engine.pair_state(pair)
    assert state.issued == {"c1"}
    assert state.history == ["Спросите партнёра о детстве"]

    reply_done = service.done("u1")
    assert "/rate" in reply_done
    assert state.completed == {"c1"}

    reply_rate = service.rate("u1", 5)
    assert "учтена" in reply_rate
    assert service.rating_log == [
        {"pair_id": pair.id, "card_id": "c1", "user_id": "u1", "score": 5}
    ]


def test_rate_scale_and_done_order_enforced():
    service = make_service()
    pair_up(service)
    with pytest.raises(NotIssued):
        service.rate("u2", 5)  # нечего оценивать
    service.today("u1")
    with pytest.raises(BotError):
        service.rate("u2", "отлично")  # нечисловая оценка
    done_second = service.done("u2")  # /done любым из пары — карточка общая
    assert "/rate" in done_second
    service.rate("u2", 3)
    with pytest.raises(BotError):
        service.rate("u2", 6)


def test_done_twice_rejected_with_not_issued():
    """Повторный /done после уже отмеченной карточки — ошибка, не двойной учёт."""
    service = make_service()
    pair_up(service)
    service.today("u1")
    service.done("u1")
    with pytest.raises(NotIssued):
        service.done("u1")


def test_free_policy_daily_gate_server_decides():
    service = make_service()
    pair_up(service)
    service.today("u1")
    # free: интервал решает сервер (1/день), юзер не выдаёт себе вторую
    with pytest.raises(AlreadyIssued):
        service.today("u1")
    with pytest.raises(AlreadyIssued):
        service.today("u2")  # карточка пара, а не юзера


def test_no_cards_left_maps_to_engine_error():
    service = make_service(cards=[make_card("c1", "Один")])
    pair_up(service)
    service.today("u1")
    service.done("u1")
    # сегодня уже выдано; проверяем NoCardAvailable напрямую (вышел интервал)
    with pytest.raises(NoCardAvailable):
        service._engine.issue_card(service.pair_for("u1"))


# -- dry-run fallback ----------------------------------------------------------


def test_no_token_means_dry_run_log_sender(monkeypatch, caplog):
    monkeypatch.delenv(bot_module.TOKEN_ENV, raising=False)
    assert get_token() is None
    with caplog.at_level("INFO", logger="dardeck.bots"):
        sender = bot_module.default_sender()
    assert isinstance(sender, LogSender)
    assert not isinstance(sender, TelegramSender)
    assert "dry-run" in caplog.text


def test_token_present_selects_telegram_sender(monkeypatch):
    monkeypatch.setenv(bot_module.TOKEN_ENV, "42:TESTTOKEN")
    sender = bot_module.default_sender()
    assert isinstance(sender, TelegramSender)


def test_log_sender_dry_run_replaces_sending(monkeypatch, caplog):
    """Полный флоу с dry-run Sender: карточки вместо отправки пишутся в лог."""
    monkeypatch.delenv(bot_module.TOKEN_ENV, raising=False)
    sender = LogSender()
    service = make_service(sender=sender)
    pair_up(service)
    with caplog.at_level("INFO", logger="dardeck.bots"):
        service.today("u1")
        service.done("u1")
        service.rate("u1", 5)
    # в сухом режиме «отправлено» только в лог
    assert len(sender.records) == 2
    assert sender.records == [("u1", "c1"), ("u2", "c1")]
    assert "dry-run" in caplog.text
    assert service.rating_log[0]["score"] == 5


def test_module_imports_without_token_and_without_aiogram_top_level():
    """Импорт модуля бота без токена: aiogram не тянется на верхний уровень."""
    env = dict(__import__("os").environ)
    env.pop(bot_module.TOKEN_ENV, None)
    code = (
        "import sys; sys.path.insert(0, r'{root}'); "
        "import bots.telegram.bot as m; "
        "assert m.get_token() is None; "
        "assert 'aiogram' not in sys.modules; print('ok')"
    ).format(root=REPO_ROOT)
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "ok"


def test_router_registers_five_commands():
    aiogram = pytest.importorskip("aiogram")
    from aiogram.filters import Command, CommandStart

    router = bot_module.build_router(make_service())
    handlers = router.message.handlers
    assert len(handlers) == 5
    filter_objects = [f for h in handlers for f in getattr(h, "filters", [])]
    # фильтры обёрнуты в FilterObject — сам Command лежит в .callback
    inner = [f.callback for f in filter_objects]
    commands = [c for f in inner for c in getattr(f, "commands", [])]
    assert commands == ["start", "pair", "today", "done", "rate"]
    assert any(isinstance(f, CommandStart) for f in inner)
