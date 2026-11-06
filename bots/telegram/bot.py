#!/usr/bin/env python
"""Telegram-бот поверх движка Dardeck (aiogram 3) — тонкий адаптер (Task 4).

Разделение слоёв:
- движок: чистая библиотека, выдача issue_card(pair)/учёт mark_completed,
  состояние пары (PairState) живёт в движке in-memory — адаптер его не пишет
  и не дублирует, решает только какие команды что вызывают;
- DeliveryPolicy: решение сервера (free: 1 карточка/день) — адаптер читает
  engine.policy.interval для гейта /today, но не даёт юзеру управлять им;
  премиум-член пары расслабляет интервал через resolve_policy (premium-дни);
- UGC-движок: mark_issued после выдачи (антифарм) + /like /dislike /report
  поверх последней выданной карточки пары (голос — у каждого участника);
- RatingEngine: журнал оценок /rate (4-5 -> +1, 3 -> 0, 1-2 -> -1), снимок
  журнала пишется JSON-файлом после каждой оценки (DARDECK_RATINGS_SNAPSHOT);
- Sender: доставка карточки юзеру. Токен — только env DARDECK_BOT_TOKEN;
  без токена бот в dry-run (LogSender пишет в лог вместо отправки).

aiogram импортируется ленивый (внутри функций): модуль импортируется без
токена, без aiogram установленного и без сети — чистая служебная логика
(BotService) покрыта тестами на MockSender.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from engine.engine import Engine, NoCardAvailable
from engine.models import Card, Pair, User
from engine.policy import (
    PREMIUM_MIN_INTERVAL,
    free_policy,
    premium_policy,
    resolve_policy,
)
from engine.ratings import AlreadyRated, RatingEngine
from engine.ugc import (
    AlreadyVoted,
    CardNotIssued,
    NotVotable,
    UgcEngine,
    VoteKind,
)

from ..sender import LogSender, Sender

TOKEN_ENV = "DARDECK_BOT_TOKEN"
DB_PATH_ENV = "DARDECK_DB_PATH"
RATINGS_SNAPSHOT_ENV = "DARDECK_RATINGS_SNAPSHOT"
DEFAULT_DB_PATH = "dardeck.db"  # по умолчанию файл (не :memory:) — персистентность
RATINGS_SNAPSHOT_FILE = "ratings.json"  # снимок оценок рядом с файлом БД

logger = logging.getLogger("dardeck.bots")


class BotError(Exception):
    """Базовая ошибка бота: переводится в понятный текст юзеру."""


class NoPair(BotError):
    """У юзера ещё нет пары — команда требует спаренных пользователей."""


class AlreadyIssued(BotError):
    """Серверный интервал выдачи не прошёл (free: 1 карточка в день)."""


class NotIssued(BotError):
    """Нет карточки к операции (/done без /today либо повторный /done)."""


def default_db_path() -> str:
    """Путь к sqlite-пулу карточек: env DARDECK_DB_PATH или файл dardeck.db."""
    return os.environ.get(DB_PATH_ENV) or DEFAULT_DB_PATH


def default_ratings_path(db_path: str) -> str:
    """Снимок журнала оценок по умолчанию: рядом с файлом БД (Dardeck-ratings)."""
    return str(Path(db_path).with_name(RATINGS_SNAPSHOT_FILE))


def ratings_snapshot_path(db_path: str) -> str:
    """Куда писать снимок оценок: env DARDECK_RATINGS_SNAPSHOT иначе рядом с БД."""
    env = os.environ.get(RATINGS_SNAPSHOT_ENV)
    return env if env else default_ratings_path(db_path)


# -- сервисная прослойка (без aiogram; движок + Sender) ----------------------


class BotService:
    """Команды бота поверх движка: реестр юзеров/пар в памяти адаптера.

    Единственное адаптерное состояние — идентификация (кто с кем в паре,
    какие карточки уходили паре): команды /done /rate /like /dislike /report
    в Telegram безымянны, привязать их к конкретной карточке иначе нечем.
    Состояние показов пары (PairState) держит движок и не дублируется здесь.
    """

    def __init__(
        self,
        engine: Engine,
        sender: Sender,
        ugc: UgcEngine,
        ratings: RatingEngine,
        ratings_path: str | Path | None = None,
    ) -> None:
        self._engine = engine
        self._sender = sender
        self._ugc = ugc  # антифарм голосов + промоушен/жалобы (engine.ugc)
        self._ratings = ratings  # журнал оценок /rate (engine.ratings)
        self._ratings_path = None if ratings_path is None else str(ratings_path)
        self._users: dict[str, User] = {}
        self._pairs: dict[str, Pair] = {}  # user_id -> Pair
        self._pending: list[str] = []  # юзеры в ожидании спаривания
        self._last_card: dict[str, Card] = {}  # pair_id -> последняя выданная карточка
        self._issued: dict[str, list[Card]] = {}  # pair_id -> выданные карточки (по порядку)
        self._last_issue_at: dict[str, datetime] = {}  # pair_id -> момент последней выдачи


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
            "4) /rate 1..5 — оценить карточку\n"
            "5) /like /dislike /report — реакция на карточку"
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
        stamped = now or datetime.now()
        self._ensure_daily_gate(pair, stamped)

        card = self._engine.issue_card(pair)
        self._last_card[pair.id] = card
        self._issued.setdefault(pair.id, []).append(card)
        self._last_issue_at[pair.id] = stamped
        # антифарм UGC: получатели карточки = оба участника пары (только они голосуют)
        if self._ugc.get(card.id) is None:
            # seed-карточки не создаются через UgcEngine: в реестр их добавляет
            # выдача, чтобы /like /dislike /report по ним резолвились
            self._ugc.state.cards[card.id] = card
        self._ugc.mark_issued(pair, card)
        for u in pair.users:
            self._sender.send(u, card)
        return "Карточка дня отправлена вам обоим. Выполнили — ставьте /done."

    def done(self, user_id: str) -> str:
        """/done — отметить выполненной последнюю выданную карточку пары."""
        pair = self._pair_of(user_id)
        card = self._last_card.get(pair.id)
        state = self._engine.pair_state(pair)
        if card is None or card.id not in state.issued:
            raise NotIssued("Сначала возьмите карточку дня командой /today.")
        if card.id in state.completed:
            raise NotIssued("Эта карточка уже отмечена выполненной — повторный /done не нужен.")
        self._engine.mark_completed(pair, card)
        return 'Отметили готово — молодец! Оцените карточку: /rate 1..5.'

    def rate(self, user_id: str, score: str | int) -> str:
        """/rate N — оценка последней выданной карточки пары (4-5:+1, 3:0, 1-2:-1)."""
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
        mapped = 1 if value >= 4 else (0 if value == 3 else -1)
        try:
            rating_value = self._ratings.rate(user_id, card, mapped).value
        except AlreadyRated:
            raise BotError("Вы уже оценили эту карточку — одна оценка на юзера.")
        self._dump_ratings_snapshot()
        logger.info("rate: card=%r user=%r score=%r -> %+d", card.id, user_id, value, rating_value)
        return "Спасибо, оценка учтена!"

    def like(self, user_id: str, text_sub: str | None = None) -> str:
        """/like [текст] — лайк последней выданной карточки (или найденной по подстроке)."""
        return self._vote(user_id, text_sub, VoteKind.LIKE)

    def dislike(self, user_id: str, text_sub: str | None = None) -> str:
        """/dislike [текст] — дизлайк последней выданной карточки (антифарм — в ugc)."""
        return self._vote(user_id, text_sub, VoteKind.DISLIKE)

    def report(self, user_id: str, text_sub: str | None = None) -> str:
        """/report [текст] — жалоба на последнюю выданную карточку (>=3 -> retired)."""
        pair = self._pair_of(user_id)
        card = self._vote_target(pair, text_sub)
        try:
            self._ugc.report(self._users[user_id], card)
        except KeyError:
            raise NotIssued("Эта карточка движку UGC неизвестна — голос не принят.")
        return "Жалоба отправлена, спасибо."

    # -- внутреннее -----------------------------------------------------------

    def _vote(self, user_id: str, text_sub: str | None, kind: VoteKind) -> str:
        """Голос за карточку: лайк/дизлайк; антифарм (получатели, 1 голос) — в UGC."""
        pair = self._pair_of(user_id)
        card = self._vote_target(pair, text_sub)
        voter = self._users[user_id]
        try:
            if kind is VoteKind.LIKE:
                self._ugc.like(voter, card)
            else:
                self._ugc.dislike(voter, card)
        except CardNotIssued:
            raise BotError("Эту карточку вы не получали — голос не принят.")
        except AlreadyVoted:
            raise BotError("Вы уже голосовали за эту карточку — один голос на участника.")
        except NotVotable:
            raise BotError("Карточка снята из выдачи — голосовать за неё больше нельзя.")
        except KeyError:
            raise NotIssued("Эта карточка движку UGC неизвестна — голос не принят.")
        if kind is VoteKind.LIKE:
            return "Спасибо! Лайк учтён."
        return "Дизлайк учтён."

    def _vote_target(self, pair: Pair, text_sub: str | None) -> Card:
        """Карточка для голоса: последняя выданная паре; с подстрокой — по тексту."""
        issued = self._issued.get(pair.id, [])
        if not issued:
            raise NotIssued("Нечего оценивать — сначала /today.")
        if text_sub:
            for card in reversed(issued):
                if text_sub in card.text:
                    return card
            raise NotIssued("Среди выданных вам карточек такого текста нет.")
        return issued[-1]

    def _dump_ratings_snapshot(self) -> None:
        """Снимок журнала оценок в JSON (путь из env/рядом с БД) — для tools/cleanup."""
        if self._ratings_path is None:
            return
        with open(self._ratings_path, "w", encoding="utf-8") as fh:
            json.dump(self._ratings.to_records(), fh, ensure_ascii=False)

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
        """Серверный интервал до новой выдачи — политика пары premium-aware:

        - free-пара (по умолчанию): интервал движка (engine.policy.interval),
          поведение бесплатного режима без изменений;
        - премиум-член пары: resolve_policy даёт premium-режим — интервал
          расслабляется до минимального премиум-лимита (грант премиум-дня,
          например автопромоушеном, позволяет выдавать карточки чаще).
        """
        last = self._last_issue_at.get(pair.id)
        if last is None:
            return
        policies = [resolve_policy(u, now) for u in pair.users]
        if any(p.allow_user_control for p in policies):
            interval = premium_policy(interval=PREMIUM_MIN_INTERVAL).interval
        else:
            interval = self._engine.policy.interval
        if now - last < interval:
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
        self._tasks: set[asyncio.Task] = set()  # strong ref: fire-and-forget task не соберёт GC

    def send(self, user: User, card: Card) -> None:
        text = format_card(card)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            task = loop.create_task(self._send(user.id, text))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        else:
            asyncio.run(self._send(user.id, text))

    async def _send(self, chat_id: str, text: str) -> None:
        """Доставка в Telegram: сбои не роняют хендлер, но видны в логе."""
        try:
            await self._bot.send_message(chat_id=chat_id, text=text)
        except Exception:
            logger.exception("не удалось доставить карточку в chat %s", chat_id)


def build_router(service: BotService) -> Any:
    """aiogram-роутер с командами /start /pair /today /done /rate /like /dislike /report.

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
        score = _first_arg(message)
        await _answer(message, lambda uid: service.rate(uid, score))

    @router.message(Command("like"))
    async def cmd_like(message: Any) -> None:
        sub = _first_arg(message) or None
        await _answer(message, lambda uid: service.like(uid, sub))

    @router.message(Command("dislike"))
    async def cmd_dislike(message: Any) -> None:
        sub = _first_arg(message) or None
        await _answer(message, lambda uid: service.dislike(uid, sub))

    @router.message(Command("report"))
    async def cmd_report(message: Any) -> None:
        sub = _first_arg(message) or None
        await _answer(message, lambda uid: service.report(uid, sub))

    return router


def _first_arg(message: Any) -> str:
    """Аргумент команды (текст после /cmd) — пустая строка, если нет."""
    args = (message.text or "").split(maxsplit=1)
    return args[1] if len(args) > 1 else ""


async def _answer(message: Any, action: Callable[[str], str]) -> None:
    """Обёртка хендлеров: ошибки сервиса/движка -> текст юзеру, без стеков."""
    try:
        await message.answer(action(str(message.from_user.id)))
    except BotError as exc:
        await message.answer(str(exc))
    except NoCardAvailable:
        await message.answer("Новых карточек пока нет — попробуйте позже.")


def main() -> None:
    """Сборка и запуск: движок (sqlite-пул карточек), UGC/оценки + Sender по токену.

    Без токена BotService собирается с LogSender и режим dry-run показывается
    в логе; polling без токена невозможен, долгий процесс не поднимаем.
    """
    # Видимость логов в docker (stderr не tty): INFO иначе теряется (lastResort).
    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )

    from aiogram import Dispatcher

    from adapters.db.sqlite import SqliteCardSource
    from adapters.rules.rotation_rule import RotationRule

    sender = default_sender()
    db_path = default_db_path()  # по умолчанию файл dardeck.db (env DARDECK_DB_PATH)
    source = SqliteCardSource(db_path)
    engine = Engine(source, RotationRule(), policy=free_policy())  # free: сервер решает
    ratings = RatingEngine()
    service = BotService(
        engine,
        sender,
        ugc=UgcEngine(),
        ratings=ratings,
        ratings_path=ratings_snapshot_path(db_path),
    )

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


if __name__ == "__main__":
    main()
