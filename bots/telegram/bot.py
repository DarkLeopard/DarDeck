"""Telegram-бот поверх движка Dardeck (aiogram 3) — тонкий адаптер (Task 4).

Разделение слоёв:
- движок: чистая библиотека, выдача issue_card(pair)/учёт mark_completed,
  состояние пары (PairState) живёт в движке in-memory — адаптер его не пишет
  и не дублирует, решает только какие команды что вызывают;
- DeliveryPolicy: решение сервера (free: 1 карточка/день) — адаптер читает
  engine.policy.interval для гейта /today, но не даёт юзеру управлять им;
- Sender: доставка карточки юзеру. Токен — только env DARDECK_BOT_TOKEN;
  без токена бот в dry-run (LogSender пишет в лог вместо отправки).

aiogram импортируется ленивый (внутри функций): модуль импортируется без
токена, без aiogram установленного и без сети — чистая служебная логика
(BotService) покрыта тестами на MockSender.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from typing import Any, Callable

from engine.engine import Engine, NoCardAvailable
from engine.models import Card, Pair, User
from engine.policy import free_policy

from ..sender import LogSender, Sender

TOKEN_ENV = "DARDECK_BOT_TOKEN"
DB_PATH_ENV = "DARDECK_DB_PATH"

logger = logging.getLogger("dardeck.bots")


class BotError(Exception):
    """Базовая ошибка бота: переводится в понятный текст юзеру."""


class NoPair(BotError):
    """У юзера ещё нет пары — команда требует спаренных пользователей."""


class AlreadyIssued(BotError):
    """Серверный интервал выдачи не прошёл (free: 1 карточка в день)."""


class NotIssued(BotError):
    """Нет карточки к операции (/done без /today либо повторный /done)."""


# -- сервисная прослойка (без aiogram; движок + Sender) ----------------------


class BotService:
    """Команды бота поверх движка: реестр юзеров/пар в памяти адаптера.

    Единственное адаптерное состояние — идентификация (кто с кем в паре,
    какая карточка последней уходила): команды /done и /rate в Telegram
    безымянны, привязать их к конкретной карточке иначе нечем. Состояние
    показов пары (PairState) держит движок и не дублируется здесь.
    """

    def __init__(self, engine: Engine, sender: Sender) -> None:
        self._engine = engine
        self._sender = sender
        self._users: dict[str, User] = {}
        self._pairs: dict[str, Pair] = {}  # user_id -> Pair
        self._pending: list[str] = []  # юзеры в ожидании спаривания
        self._last_card: dict[str, Card] = {}  # pair_id -> последняя выданная карточка
        self._last_issue_at: dict[str, datetime] = {}  # pair_id -> момент последней выдачи
        self.rating_log: list[dict[str, Any]] = []  # лог оценок (/rate) — MVP-лог


    # -- команды ------------------------------------------------------------

    def start(self, user_id: str, username: str | None) -> str:
        """/start — регистрация юзера."""
        user = self._get_user(user_id, username)
        return (
            "Привет! Dardeck — карточки для вашего вечера.\n"
            "Шаги:\n"
            "1) /pair — стать в пару (оба партнёра должны выполнить)\n"
            "2) /today — получить карточку дня\n"
            "3) /done — отметить, что карточка выполнена\n"
            "4) /rate 1..5 — оценить карточку"
        )

    def pair(self, user_id: str, username: str | None) -> str:
        """/pair — спаривание из двух половинок: первый ждёт, второй замыкает."""
        user = self._get_user(user_id, username)
        if user.id in self._pairs:
            return "Вы уже в паре."
        if user.id in self._pending:
            return "Уже ждём второго партнёра — пусть напишет /pair."
        if not self._pending:
            self._pending.append(user.id)
            return (
                "Заявка на пару принята. Второй партнёр должен вызвать /pair "
                "в этом боте — свяжемся автоматически."
            )
        partner_id = self._pending.pop(0)
        partner = self._users[partner_id]
        pair_id = f"pair:{min(user.id, partner.id)}:{max(user.id, partner.id)}"
        pair = Pair(id=pair_id, user_a=partner, user_b=user)
        self._pairs[user.id] = pair
        self._pairs[partner.id] = pair
        return "Вы в паре! Пишите /today, чтобы взять карточку."

    def today(self, user_id: str, now: datetime | None = None) -> str:
        """/today — выдать карточку дня: движок решает, Sender доставляет обоим."""
        pair = self._pair_of(user_id)
        self._ensure_daily_gate(pair, now or datetime.now())

        card = self._engine.issue_card(pair)
        self._last_card[pair.id] = card
        self._last_issue_at[pair.id] = now or datetime.now()
        for u in pair.users:
            self._sender.send(u, card)
        return "Карточка дня отправлена вам обоим. Выполнили — ставьте /done."

    def done(self, user_id: str) -> str:
        """/done — отметить выполненной последнюю выданную карточку пары."""
        pair = self._pair_of(user_id)
        card = self._last_card.get(pair.id)
        if card is None or card.id not in self._engine.pair_state(pair).issued:
            raise NotIssued("Сначала возьмите карточку дня командой /today.")
        self._engine.mark_completed(pair, card)
        return 'Отметили готово — молодец! Оцените карточку: /rate 1..5.'

    def rate(self, user_id: str, score: str | int) -> str:
        """/rate N — лог оценки последней выданной карточки пары."""
        pair = self._pair_of(user_id)
        card = self._last_card.get(pair.id)
        if card is None:
            raise NotIssued("Оценивать тут нечего — сначала /today.")
        try:
            value = int(score)
        except (TypeError, ValueError):
            raise BotError("Оценка — целое число: /rate 1..5.")
        if not 1 <= value <= 5:
            raise BotError("Оценка по шкале 1..5, попробуйте заново.")
        entry = {
            "pair_id": pair.id,
            "card_id": card.id,
            "user_id": user_id,
            "score": value,
        }
        self.rating_log.append(entry)
        logger.info("rate logged: %s", entry)
        return "Спасибо, оценка учтена!"

    # -- для адаптеров/тестов -------------------------------------------------

    def pair_for(self, user_id: str) -> Pair | None:
        """Пара юзера (или None) — публичный доступ для нижнего слоя/тестов."""
        return self._pairs.get(user_id)

    # -- внутреннее -----------------------------------------------------------

    def _get_user(self, user_id: str, username: str | None) -> User:
        user = self._users.get(user_id)
        if user is None:
            user = User(id=user_id, username=username or user_id)
            self._users[user_id] = user
        return user

    def _pair_of(self, user_id: str) -> Pair:
        pair = self._pairs.get(user_id)
        if pair is None:
            raise NoPair("Вы не в паре. Начните с /pair — обоим партнёрам нужно вызвать.")
        return pair

    def _ensure_daily_gate(self, pair: Pair, now: datetime) -> None:
        """Free-режим: серверный интервал (engine.policy.interval) до новой выдачи."""
        last = self._last_issue_at.get(pair.id)
        if last is not None and now - last < self._engine.policy.interval:
            raise AlreadyIssued(
                "Карточка уже была за сегодня — следующая по серверному интервалу."
            )


# -- Telegram-слой (aiogram импортируется лениво) -----------------------------


def format_card(card: Card) -> str:
    """Формат текста сообщения с карточкой."""
    return f"🔥 {card.theme}\n\n{card.text}"


def get_token() -> str | None:
    """Токен только из env DARDECK_BOT_TOKEN (не пустой)."""
    return os.environ.get(TOKEN_ENV) or None


def default_sender() -> Sender:
    """Sender по наличию токена: TelegramSender либо dry-run LogSender."""
    token = get_token()
    if not token:
        logger.info("%s не задан — dry-run: карточки пишутся в лог, не отправляются", TOKEN_ENV)
        return LogSender()
    from aiogram import Bot

    return TelegramSender(Bot(token=token))


class TelegramSender(Sender):
    """Sender поверх aiogram.Bot: send(user, card) -> send_message(user.id).

    Контракт Sender синхронный (MockSender/LogSender), а aiogram — async:
    в-running-цикле планируем корутину task'ой (aiogram-хендлеры — async),
    вне цикла (скриптовый запуск) — до-гоняем asyncio.run.
    """

    def __init__(self, bot: Any) -> None:
        self._bot = bot

    def send(self, user: User, card: Card) -> None:
        text = format_card(card)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            loop.create_task(self._send(user.id, text))
        else:
            asyncio.run(self._send(user.id, text))

    async def _send(self, chat_id: str, text: str) -> None:
        await self._bot.send_message(chat_id=chat_id, text=text)


def build_router(service: BotService) -> Any:
    """aiogram-роутер с командами /start /pair /today /done /rate.

    Хендлеры тонкие: разбор аргумента команды + перевод ошибок
    (BotError/NoCardAvailable) в понятный текст. aiogram импортируется здесь.
    """
    from aiogram import Router
    from aiogram.filters import Command, CommandStart

    router = Router(name="dardeck")

    @router.message(CommandStart())
    async def cmd_start(message: Any) -> None:
        reply = service.start(str(message.from_user.id), message.from_user.username)
        await message.answer(reply)

    @router.message(Command("pair"))
    async def cmd_pair(message: Any) -> None:
        reply = service.pair(str(message.from_user.id), message.from_user.username)
        await message.answer(reply)

    @router.message(Command("today"))
    async def cmd_today(message: Any) -> None:
        await _answer(message, lambda uid: service.today(uid))

    @router.message(Command("done"))
    async def cmd_done(message: Any) -> None:
        await _answer(message, service.done)

    @router.message(Command("rate"))
    async def cmd_rate(message: Any) -> None:
        args = (message.text or "").split(maxsplit=1)
        score = args[1] if len(args) > 1 else ""
        await _answer(message, lambda uid: service.rate(uid, score))

    return router


async def _answer(message: Any, action: Callable[[str], str]) -> None:
    """Обёртка хендлеров: ошибки сервиса/движка -> текст юзеру, без стеков."""
    try:
        await message.answer(action(str(message.from_user.id)))
    except BotError as exc:
        await message.answer(str(exc))
    except NoCardAvailable:
        await message.answer("Новых карточек пока нет — попробуйте позже.")


def main() -> None:
    """Сборка и запуск: движок (sqlite-пул карточек) + Sender по токену.

    Без токена BotService собирается с LogSender и режим dry-run показывается
    в логе; polling без токена невозможен, долгий процесс не поднимаем.
    """
    from aiogram import Dispatcher

    from adapters.db.sqlite import SqliteCardSource
    from adapters.rules.rotation_rule import RotationRule

    sender = default_sender()
    source = SqliteCardSource(os.environ.get(DB_PATH_ENV) or ":memory:")
    engine = Engine(source, RotationRule(), policy=free_policy())  # free: сервер решает
    service = BotService(engine, sender)

    if isinstance(sender, LogSender):
        logger.info("dry-run сервис собран; polling не запускается (нет токена)")
        return

    dp = Dispatcher()
    dp.include_router(build_router(service))
    logger.info("Dardeck bot стартует (long polling)")
    dp.run_polling(get_bot(sender))


def get_bot(sender: Sender) -> Any:
    """Bot для polling'а: TelegramSender носит его в себе (или исключение)."""
    if not isinstance(sender, TelegramSender):
        raise RuntimeError("polling доступен только с TelegramSender (нужен токен)")
    return sender._bot
