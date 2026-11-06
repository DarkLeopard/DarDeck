"""Интеграционные тесты бота поверх движка: MockSender, без сети и токена.

Покрытие Task 4/7:
- пара юзеров: /pair -> /today (карточка обоим) -> /done -> /rate (журнал оценок);
- UGC-проводка: mark_issued после выдачи, /like /dislike /report (антифарм);
- гейт свободного интервала (DeliveryPolicy: сервер решает, 1/день);
- премиум-грант: интервал расслабляется (shorter interval becomes possible);
- снимок оценок пишется JSON-файлом после /rate (DARDECK_RATINGS_SNAPSHOT);
- dry-run fallback: без DARDECK_BOT_TOKEN -> LogSender instead of TelegramSender;
- модуль импортируется без токена и без aiogram на верхнем уровне (subprocess);
- кнопки (Task 9): reply-клавиатура меню, inline-кнопки карточки с
  callback_data 'act:<action>:<card_id>', callback-флоу через BotService,
  «Мои карточки» (последние до 3).
"""

import asyncio
import json
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

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
    ratings_snapshot_path,
)
from engine.engine import Engine, NoCardAvailable
from engine.models import Card, CardStatus, Pair, SourceKind, User
from engine.ratings import RatingEngine
from engine.ugc import UgcEngine, grant_premium_day

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
                 cards: list[Card] | None = None,
                 ratings_path: str | None = None,
                 test_users: set[str] | None = None) -> BotService:
    deck = cards if cards is not None else [
        make_card("c1", "Спросите партнёра о детстве"),
        make_card("c2", "Свидание на выходные вместе"),
        make_card("c3", "Мечта, о которой вы не рассказывали"),
    ]
    engine = Engine(ListSource(deck), RotationRule())
    return BotService(
        engine,
        sender if sender is not None else MockSender(),
        ugc=UgcEngine(),
        ratings=RatingEngine(),
        ratings_path=ratings_path,
        test_users=test_users,
    )


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
    # оценка 1..5 маппится в журнал RatingEngine (+1/0/-1), 5 -> +1
    assert service._ratings.to_records() == [
        {"card_id": "c1", "user_id": "u1", "value": 1, "text": None}
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


def test_premium_grant_relaxes_interval_for_that_pair():
    """Грант премиум-дня: интервал пары расслабляется — выдача раньше суток.

    Free-пара против той же задержки — по-прежнему AlreadyIssued.
    """
    service = make_service(cards=[
        make_card("c1", "Первая"),
        make_card("c2", "Вторая"),
    ])
    pair_up(service)
    now = datetime(2026, 11, 6, 12, 0, 0)

    service.today("u1", now)
    pair = service.pair_for("u1")
    later = now + timedelta(hours=1, seconds=1)  # больше премиум-минимума, меньше суток

    # контроль: без премиума интервал свободного режима не менялся
    free_service = make_service(cards=[make_card("f1", "Свободная")])
    pair_up(free_service)
    free_service.today("u1", now)
    with pytest.raises(AlreadyIssued):
        free_service.today("u1", later)

    grant_premium_day(pair.user_a, now=now)  # например, автопромоушен UGC
    reply = service.today("u2", later)  # премиум-пара: выдача раньше суток возможна
    assert "Карточка дня" in reply


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
    assert service._ratings.to_records()[0]["value"] == 1


# -- UGC-проводка: mark_issued + /like /dislike /report ------------------------


def test_today_marks_issued_receivers_to_ugc():
    """После /today получатели карточки зарегистрированы в UGC (антифарм)."""
    service = make_service()
    pair_up(service)
    service.today("u1")
    card = service._last_card[service.pair_for("u1").id]
    receivers = service._ugc.state.issued_to[card.id]
    assert receivers == {"u1", "u2"}


def test_like_dislike_report_on_last_issued_card():
    service = make_service(cards=[make_card("c1", "Прогулка в парке")])
    pair_up(service)
    service.today("u1")
    card_id = service._last_card[service.pair_for("u1").id].id

    assert "Лайк учтён" in service.like("u1")
    assert "Дизлайк учтён" in service.dislike("u2")
    assert "Жалоба отправлена" in service.report("u1")
    assert service._ugc.state.like_count(card_id) == 1
    assert service._ugc.state.dislike_count(card_id) == 1
    assert service._ugc.state.report_count(card_id) == 1


def test_like_targets_card_by_text_substring():
    """Аргумент /like — подстрока текста: выбираем одну из выданных карточек пары."""
    service = make_service()
    pair_up(service)
    service.today("u1")
    with pytest.raises(AlreadyIssued):
        service.today("u1")  # интервал свободного режима ещё не прошёл
    # выдача второй карточки принудительно (минуя гейт), чтобы было из чего выбирать
    card2 = make_card("c2", "Свидание на выходные вместе")
    pair = service.pair_for("u1")
    service._ugc.state.cards[card2.id] = card2  # как в today(): реестр UGC-состояния
    service._issued[pair.id].append(card2)
    service._ugc.mark_issued(pair, card2)

    service.like("u1", "Свидание")
    assert service._ugc.state.like_count("c2") == 1
    assert service._ugc.state.like_count("c1") == 0


def test_like_without_card_raises_not_issued():
    service = make_service()
    pair_up(service)
    with pytest.raises(NotIssued):
        service.like("u1")
    with pytest.raises(NotIssued):
        service.report("u1")


def test_double_vote_mapped_to_friendly_error():
    service = make_service()
    pair_up(service)
    service.today("u1")
    service.like("u1")
    with pytest.raises(BotError, match="уже голосовали"):
        service.like("u1")
    with pytest.raises(BotError, match="уже голосовали"):
        service.dislike("u1")


def test_unknown_substring_mapped_to_friendly_error():
    service = make_service()
    pair_up(service)
    service.today("u1")
    with pytest.raises(NotIssued, match="такого текста нет"):
        service.like("u1", "несуществующий текст")


# -- /rate: маппинг шкалы 1..5 -> +1/0/-1 и снимок журнала ---------------------


def test_rate_scale_maps_5_3_1(monkeypatch):
    """Шкала бота 1..5 маппится в журнал +1/0/-1: 5->+1, 3->0, 1->-1."""
    monkeypatch.delenv(bot_module.RATINGS_SNAPSHOT_ENV, raising=False)
    service = make_service()
    pair_up(service)
    service.today("u1")
    pair = service.pair_for("u1")
    service.rate("u1", 5)  # 5 -> +1, и одну оценку на карточку юзер ставит один раз

    card2, card3 = make_card("c2", "Свидание на выходные"), make_card("c3", "Третья")
    service._issued[pair.id].extend([card2, card3])
    service._last_card[pair.id] = card3  # нейтральная — на c3
    service.rate("u2", 3)
    service._last_card[pair.id] = card2  # негативная — на c2
    service.rate("u2", 1)

    by_card: dict[str, dict[str, int]] = {}
    for record in service._ratings.to_records():
        by_card.setdefault(record["card_id"], {})[record["user_id"]] = record["value"]
    assert by_card["c1"] == {"u1": 1}
    assert by_card["c2"] == {"u2": -1}
    assert by_card["c3"] == {"u2": 0}


def test_rate_twice_same_user_rejected_friendly():
    """Повторная оценка той же карточки тем же юзером — одна на юзера (RatingEngine)."""
    service = make_service()
    pair_up(service)
    service.today("u1")
    service.rate("u1", 4)
    with pytest.raises(BotError, match="оценк"):
        service.rate("u1", 2)  # изменять оценку нельзя: одна на юзера


def test_rate_beyond_scale_errors():
    service = make_service()
    pair_up(service)
    service.today("u1")
    with pytest.raises(BotError):
        service.rate("u1", "отлично")
    with pytest.raises(BotError):
        service.rate("u1", 6)


def test_rate_dumps_ratings_snapshot(monkeypatch, tmp_path):
    """После /rate журнал выгружается в JSON по DARDECK_RATINGS_SNAPSHOT."""
    snapshot = tmp_path / "ratings.json"
    monkeypatch.setenv(bot_module.RATINGS_SNAPSHOT_ENV, str(snapshot))
    service = make_service(ratings_path=ratings_snapshot_path("dardeck.db"))
    pair_up(service)
    service.today("u1")
    service.rate("u1", 5)

    records = json.loads(snapshot.read_text(encoding="utf-8"))
    assert records == [
        {"card_id": "c1", "user_id": "u1", "value": 1, "text": None}
    ]


def test_default_ratings_path_beside_db_file(monkeypatch):
    """Снимок по умолчанию — рядом с файлом БД; env DARDECK_RATINGS_SNAPSHOT сильнее."""
    monkeypatch.delenv(bot_module.RATINGS_SNAPSHOT_ENV, raising=False)
    monkeypatch.delenv(bot_module.DB_PATH_ENV, raising=False)
    assert bot_module.default_db_path() == "dardeck.db"
    assert bot_module.default_ratings_path("dardeck.db") == "ratings.json"
    assert bot_module.default_ratings_path("data/deck.sqlite") == str(
        Path("data") / "ratings.json"
    )
    monkeypatch.setenv(bot_module.RATINGS_SNAPSHOT_ENV, "custom.json")
    assert ratings_snapshot_path("any.db") == "custom.json"


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


def test_router_registers_commands_with_menu_buttons():
    aiogram = pytest.importorskip("aiogram")
    from aiogram.filters import Command, CommandStart

    router = bot_module.build_router(make_service())
    handlers = router.message.handlers
    assert len(handlers) == 14  # 11 команд + кнопки «Дать фант»/«Помощь»/«Мои карточки»
    assert len(router.callback_query.handlers) == 1  # inline-кнопки карточки
    filter_objects = [f for h in handlers for f in getattr(h, "filters", [])]
    # фильтры обёрнуты в FilterObject — сам Command лежит в .callback
    inner = [f.callback for f in filter_objects]
    commands = [c for f in inner for c in getattr(f, "commands", [])]
    assert commands == [
        "start", "pair", "fant", "today", "id", "help",
        "done", "rate", "like", "dislike", "report",
    ]
    assert any(isinstance(f, CommandStart) for f in inner)


# -- Task 8: соло-режим /fant, владелец-тест (whitelist), /id -------------------


def test_parse_test_user_ids_csv_int():
    """DARDECK_TEST_USER_IDS: csv int -> set строк; мусор/пустые пропускаются."""
    assert bot_module.parse_test_user_ids(None) == set()
    assert bot_module.parse_test_user_ids("") == set()
    assert bot_module.parse_test_user_ids("111, 222") == {"111", "222"}
    assert bot_module.parse_test_user_ids("333,,abc,444") == {"333", "444"}


def test_solo_fant_issues_card_to_single_user():
    """/fant одиночке: соло-карточка уходит только ему (self-пара на движке)."""
    sender = MockSender()
    service = make_service(sender=sender)
    service.start("u1", "ann")

    reply = service.fant("u1", "ann")
    assert "Фант" in reply
    assert len(sender.sent) == 1
    assert sender.sent[0][0].id == "u1"

    # движок ведёт состояние соло-жизни на self-паре 'solo:<uid>' (учёт/no-repeat)
    solo = service._solo_pairs["u1"]
    assert solo.id == "solo:u1"
    assert solo.user_a is solo.user_b
    state = service._engine.pair_state(solo)
    assert state.issued == {"c1"}
    # реальная пара не создана: /pair по-прежнему доступен юзеру
    assert service.pair_for("u1") is None


def test_solo_fant_second_same_day_already_issued():
    """Повторный /fant в тот же день — AlreadyIssued (free: 1/день, friendly)."""
    service = make_service()
    service.start("u1", "ann")
    service.fant("u1", "ann")
    with pytest.raises(AlreadyIssued):
        service.fant("u1", "ann")


def test_whitelist_user_bypasses_gate_and_repeats_fant():
    """Whitelist (DARDECK_TEST_USER_IDS) обходит дневной гейт: /fant подряд."""
    sender = MockSender()
    service = make_service(sender=sender,
                           test_users=bot_module.parse_test_user_ids("111"))
    for step in range(3):
        reply = service.fant("111", f"u{step}")
        assert "Фант" in reply
    assert len(sender.sent) == 3
    assert [card.id for _, card in sender.sent] == ["c1", "c2", "c3"]  # no-repeat

    # контроль: юзер вне whitelist той же колоды упирается в гейт после первой
    sender2 = MockSender()
    gated = make_service(sender=sender2)
    gated.start("u1", "ann")
    gated.fant("u1", "ann")
    with pytest.raises(AlreadyIssued):
        gated.fant("u1", "ann")


def test_fant_after_pairing_delivers_to_both_partners():
    """После /pair «Дать фант» выдаёт карточку обоим партнёрам (алиас /today)."""
    sender = MockSender()
    service = make_service(sender=sender)
    pair_up(service)
    reply = service.fant("u2", "bob")
    assert "вам обоим" in reply
    assert {u.id for u, _ in sender.sent} == {"u1", "u2"}


def test_solo_fant_registers_receivers_in_ugc_and_vote_works():
    """Антифарм UGC сохраняется: solo-выдача -> mark_issued, /like юзера принимается."""
    service = make_service()
    service.start("u1", "ann")
    service.fant("u1", "ann")
    card = service._last_card["solo:u1"]
    assert service._ugc.state.issued_to[card.id] == {"u1"}

    assert "Лайк учтён" in service.like("u1")
    assert service._ugc.state.like_count(card.id) == 1


def test_solo_fant_gate_is_per_solo_pair_not_real_pair_state():
    """Соло-гейт считывается с PairState соло-жизни: реальная пара не смешивается."""
    service = make_service()
    service.start("u1", "ann")
    service.fant("u1", "ann")  # соло-выдача today
    service.pair("u1", "ann")
    service.pair("u2", "bob")
    # соло-жизни своя; парная выдача доступна сразу (отдельный pair_id)
    reply = service.today("u1")
    assert "Карточка дня" in reply
    pair = service.pair_for("u1")
    assert pair.id != "solo:u1"
    # движок помнит показы соло-жизни отдельно: у парной карточки свои получатели
    assert service._ugc.state.issued_to["c1"] == {"u1", "u2"}
    with pytest.raises(AlreadyIssued):
        service.fant("u1", "ann")  # теперь юзер в паре: гейт на парной карточке


def test_show_id_returns_telegram_id():
    """/id для любого юзера: только сам id, без секретов."""
    service = make_service()
    assert service.show_id("123456789") == "Ваш Telegram ID: 123456789"


def test_today_without_pair_after_fant_uses_solo_pair():
    """/today одиночке-без-пары: NoPair, если ещё нет соло-выдачи; после /fant — соло."""
    service = make_service()
    service.start("u1", "ann")
    with pytest.raises(NoPair):
        service.today("u1")
    service.fant("u1", "ann")
    # после соло-выдачи /today тоже работает на той же self-паре (гейт сработает)
    with pytest.raises(AlreadyIssued):
        service.today("u1")


# -- Task 9: кнопки (reply/inline), callback-флоу, «Мои карточки» ----------------


class StubMessage:
    """Обычное сообщение для прямого вызова хендлера роутера."""

    def __init__(self, user_id: str, text: str) -> None:
        self.from_user = SimpleNamespace(id=user_id, username=None)
        self.text = text
        self.answers: list[tuple[str, Any]] = []

    async def answer(self, text: str, reply_markup: Any = None) -> None:
        self.answers.append((text, reply_markup))


class StubCardMessage:
    """Сообщение-карточка: у него обновляют inline-разметку (edit_reply_markup)."""

    def __init__(self) -> None:
        self.markups: list[Any] = []

    async def edit_reply_markup(self, reply_markup: Any = None) -> None:
        self.markups.append(reply_markup)


class StubCallback:
    """CallbackQuery-заглушка: data + message + answer_callback_query."""

    def __init__(self, user_id: str, data: str | None,
                 message: StubCardMessage | None = None) -> None:
        self.from_user = SimpleNamespace(id=user_id)
        self.data = data
        self.message = message
        self.answered: list[tuple[str | None, bool]] = []

    async def answer(self, text: str | None = None, show_alert: bool = False) -> None:
        self.answered.append((text, show_alert))


class StubBot:
    """aiogram.Bot-подмена: фиксирует kwargs send_message (без сети)."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def send_message(self, chat_id: str, text: str,
                           reply_markup: Any = None) -> None:
        self.calls.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})


def handler_fn(router: Any, name: str, pool: str = "message") -> Any:
    """Хендлер роутера по имени функции (для прямого вызова на стабах)."""
    obj_pool = router.message if pool == "message" else router.callback_query
    for h in obj_pool.handlers:
        if h.callback.__name__ == name:
            return h.callback
    pytest.fail(f"хендлер {name} не зарегистрирован в роутере")


def run_handler(coro: Any) -> None:
    """Асинхронный вызов хендлера + дожидание fire-and-forget задач Sender'а."""
    async def run() -> None:
        await coro
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if pending:
            await asyncio.wait(pending)

    asyncio.run(run())


def button_texts(markup: Any) -> list[list[str]]:
    """Тексты кнопок клавиатуры построчно (reply или inline)."""
    rows = markup.keyboard if hasattr(markup, "keyboard") else markup.inline_keyboard
    return [[btn.text for btn in row] for row in rows]


def callback_datas(markup: Any) -> list[list[str]]:
    """callback_data кнопок inline-клавиатуры построчно."""
    return [[btn.callback_data for btn in row] for row in markup.inline_keyboard]


def test_main_menu_kb_and_inline_keyboards_shape_expectations():
    """Конструкторы клавиатур: маленькие функции с проверяемым набором кнопок."""
    pytest.importorskip("aiogram")

    assert button_texts(bot_module.main_menu_kb()) == [
        ["🎲 Дать фант"],
        ["Мои карточки", "Помощь"],
    ]
    card = bot_module.card_kb("c1")
    assert button_texts(card) == [["✅ Выполнено", "⭐ Оценить"]]
    assert callback_datas(card) == [["act:done:c1", "act:rate:c1"]]

    rate = bot_module.rate_kb("c1")
    assert button_texts(rate) == [["1", "2", "3", "4", "5"]]
    assert callback_datas(rate) == [[
        "act:rate1:c1", "act:rate2:c1", "act:rate3:c1", "act:rate4:c1", "act:rate5:c1",
    ]]

    reaction = bot_module.reaction_kb("c1")
    assert button_texts(reaction) == [["👍", "👎"], ["⚠ Report"]]
    assert callback_datas(reaction) == [["act:like:c1", "act:dislike:c1"], ["act:report:c1"]]


def test_start_answers_with_main_menu_reply_keyboard():
    """/start — reply-клавиатура главного меню из трёх кнопок."""
    pytest.importorskip("aiogram")
    router = bot_module.build_router(make_service())
    message = StubMessage("u1", "/start")
    run_handler(handler_fn(router, "cmd_start")(message))

    assert "Привет" in message.answers[0][0]
    assert button_texts(message.answers[0][1]) == [
        ["🎲 Дать фант"],
        ["Мои карточки", "Помощь"],
    ]


def test_telegram_sender_sends_card_with_inline_done_rate():
    """Sender-доставка карточки: сообщение несёт inline [✅ Выполнено][⭐ Оценить]."""
    pytest.importorskip("aiogram")
    stub = StubBot()
    sender = bot_module.TelegramSender(stub)
    card = make_card("c1", "Спросите партнёра о детстве")
    run_handler(sender._send("555", card))

    assert len(stub.calls) == 1
    call = stub.calls[0]
    assert call["chat_id"] == "555"
    assert call["text"] == bot_module.format_card(card)
    assert button_texts(call["reply_markup"]) == [["✅ Выполнено", "⭐ Оценить"]]
    assert callback_datas(call["reply_markup"]) == [["act:done:c1", "act:rate:c1"]]


def test_solo_fant_full_flow_card_with_inline_buttons_and_menu():
    """Соло-флоу через роутер: карточка с inline-кнопками, ответ — с меню."""
    pytest.importorskip("aiogram")
    stub = StubBot()
    service = make_service(sender=bot_module.TelegramSender(stub))
    router = bot_module.build_router(service)
    message = StubMessage("u1", "/fant")
    run_handler(handler_fn(router, "cmd_fant")(message))

    # карточка доставлена одним сообщением с inline-кнопками
    assert len(stub.calls) == 1
    markup = stub.calls[0]["reply_markup"]
    assert button_texts(markup) == [["✅ Выполнено", "⭐ Оценить"]]
    assert callback_datas(markup) == [["act:done:c1", "act:rate:c1"]]
    # сопроводительный ответ идёт с reply-клавиатурой главного меню
    assert button_texts(message.answers[0][1]) == [
        ["🎲 Дать фант"], ["Мои карточки", "Помощь"],
    ]


def test_solo_callback_flow_done_rate_like():
    """Callback-флоу: done -> подтверждение + реакции; rate 4 -> +1 в журнале."""
    pytest.importorskip("aiogram")
    service = make_service()
    service.start("u1", "ann")
    service.fant("u1", "ann")
    router = bot_module.build_router(service)
    on_callback = handler_fn(router, "on_callback", pool="callback_query")
    card_msg = StubCardMessage()

    done = StubCallback("u1", "act:done:c1", message=card_msg)
    run_handler(on_callback(done))
    assert "молодец" in done.answered[0][0]
    assert not done.answered[0][1]  # успех — без алерта, спиннер погашен
    state = service._engine.pair_state(service._solo_pairs["u1"])
    assert state.completed == {"c1"}
    # после выполнения появились [👍][👎][⚠ Report]
    assert callback_datas(card_msg.markups[-1]) == [
        ["act:like:c1", "act:dislike:c1"], ["act:report:c1"],
    ]

    # ⭐ Оценить -> второй экран 1..5
    screen = StubCallback("u1", "act:rate:c1", message=card_msg)
    run_handler(on_callback(screen))
    assert callback_datas(card_msg.markups[-1]) == [[
        f"act:rate{n}:c1" for n in range(1, 6)
    ]]
    assert "1 до 5" in screen.answered[0][0]

    rate4 = StubCallback("u1", "act:rate4:c1", message=card_msg)
    run_handler(on_callback(rate4))
    assert "учтена" in rate4.answered[0][0]
    # 4 -> +1 (маппинг волны-1), оценка приписана карточке callback'ом
    assert service._ratings.to_records() == [
        {"card_id": "c1", "user_id": "u1", "value": 1, "text": None}
    ]

    like = StubCallback("u1", "act:like:c1", message=card_msg)
    run_handler(on_callback(like))
    assert "Лайк учтён" in like.answered[0][0]
    assert service._ugc.state.like_count("c1") == 1


def test_double_done_callback_friendly_no_crash():
    """Повторный done по кнопке: дружелюбный ответ, без исключения."""
    pytest.importorskip("aiogram")
    service = make_service()
    service.start("u1", "ann")
    service.fant("u1", "ann")
    router = bot_module.build_router(service)
    on_callback = handler_fn(router, "on_callback", pool="callback_query")
    card_msg = StubCardMessage()

    run_handler(on_callback(StubCallback("u1", "act:done:c1", message=card_msg)))
    double = StubCallback("u1", "act:done:c1", message=card_msg)
    run_handler(on_callback(double))  # не бросает
    text, show_alert = double.answered[0]
    assert "уже отмечена выполненной" in text
    assert show_alert  # ошибки — алертом


def test_unknown_callback_data_friendly_error_no_crash():
    """Неизвестный/битый callback_data: friendly error, без падения."""
    pytest.importorskip("aiogram")
    service = make_service()
    service.start("u1", "ann")
    service.fant("u1", "ann")
    router = bot_module.build_router(service)
    on_callback = handler_fn(router, "on_callback", pool="callback_query")

    for data in ("act:zoom:c1", "garbage", "act:done:"):
        cq = StubCallback("u1", data)
        run_handler(on_callback(cq))  # не бросает
        text, show_alert = cq.answered[0]
        assert show_alert
        assert "возьмите новую карточку" in text
    # после мусора состояние не изменилось: карточка не выполнена
    state = service._engine.pair_state(service._solo_pairs["u1"])
    assert state.completed == set()


def test_callback_rate9_is_unknown_action_not_score():
    """'rate9' не оценка и не экран: friendly unknown, оценка не ставится."""
    pytest.importorskip("aiogram")
    service = make_service()
    service.start("u1", "ann")
    service.fant("u1", "ann")
    router = bot_module.build_router(service)
    on_callback = handler_fn(router, "on_callback", pool="callback_query")
    cq = StubCallback("u1", "act:rate9:c1")
    run_handler(on_callback(cq))
    assert "возьмите новую карточку" in cq.answered[0][0]
    assert service._ratings.to_records() == []


def test_callback_without_any_card_friendly():
    """Callback до всякой выдачи: friendly, без падения (нет соло/пары)."""
    pytest.importorskip("aiogram")
    service = make_service()
    router = bot_module.build_router(service)
    on_callback = handler_fn(router, "on_callback", pool="callback_query")
    cq = StubCallback("u1", "act:done:c1")
    run_handler(on_callback(cq))
    assert cq.answered[0][1]  # alert


def test_my_cards_lists_upto_three_latest():
    """«Мои карточки»: последние до 3 выданных юзеру, кратко, с идентификатором."""
    deck = [make_card(f"c{n}", f"Карточка номер {n}") for n in range(1, 6)]
    service = make_service(cards=deck,
                           test_users=bot_module.parse_test_user_ids("111"))
    service.start("111", "owner")
    for _ in range(5):  # whitelist: гейт обходится, выдаём подряд c1..c5
        service.fant("111", "owner")

    reply = service.my_cards("111")
    lines = reply.strip().splitlines()[1:]  # без заголовка
    assert len(lines) == 3  # только последние три
    assert "#c3" in lines[0] and "#c4" in lines[1] and "#c5" in lines[2]
    assert "#c1" not in reply and "#c2" not in reply


def test_my_cards_long_text_snipped():
    """Длинный текст карточки в «Моих карточках» — краткий сниппет (48 символов + …)."""
    deck = [make_card("c1", "длинный" * 20)]
    service = make_service(cards=deck)
    service.start("u1", "ann")
    service.fant("u1", "ann")
    reply = service.my_cards("u1")
    assert "#c1" in reply
    assert reply.count("длинный") == 6  # 48 = 6 слов по 8 символов
    assert reply.rstrip().endswith("…")


def test_my_cards_empty_is_friendly():
    """Нет выданных карточек: friendly BotError, обработчик отвечает без стека."""
    service = make_service()
    service.start("u1", "ann")
    with pytest.raises(BotError, match="Дать фант"):
        service.my_cards("u1")
