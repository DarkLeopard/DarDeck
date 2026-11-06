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
- Кнопки (Task 9): главное меню reply-keyboard на /start (Мои карточки =
  последние до 3 выданных), карточка несёт inline-кнопки
  callback_data 'act:<action>:<card_id>'; спиннер гасится всегда;
- Sender: доставка карточки юзеру с inline-кнопками карточки. Токен — только
  env DARDECK_BOT_TOKEN;
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
TEST_USER_IDS_ENV = "DARDECK_TEST_USER_IDS"
DEFAULT_DB_PATH = "dardeck.db"  # по умолчанию файл (не :memory:) — персистентность
RATINGS_SNAPSHOT_FILE = "ratings.json"  # снимок оценок рядом с файлом БД
# -- кнопки: главное меню (reply-клавиатура, Task 9) ----------------------------
FANT_BUTTON_TEXT = "🎲 Дать фант"  # кнопка клавиатуры — алиас /fant (Task 8, solo)
CARDS_BUTTON_TEXT = "Мои карточки"  # последние до 3 выданных юзеру карточек
HELP_BUTTON_TEXT = "Помощь"

# кнопки карточки (inline; callback_data = callback_of(action, card_id))
DONE_BUTTON_TEXT = "✅ Выполнено"
RATE_BUTTON_TEXT = "⭐ Оценить"
LIKE_BUTTON_TEXT = "👍"
DISLIKE_BUTTON_TEXT = "👎"
REPORT_BUTTON_TEXT = "⚠ Report"

CB_PREFIX = "act"  # callback_data = 'act:<action>:<card_id>'
CB_DONE = "done"
CB_RATE = "rate"  # открывает экран оценки (кнопки 1..5 -> 'act:rate<n>:<id>')
CB_LIKE = "like"
CB_DISLIKE = "dislike"
CB_REPORT = "report"

MY_CARDS_LIMIT = 3  # «Мои карточки»: последние до 3 выданных
CARD_SNIPPET_MAX = 48  # краткая строка текста карточки в списке

STALE_BUTTON = f"Эта кнопка устарела — возьмите новую карточку «{FANT_BUTTON_TEXT}»."
UNKNOWN_BUTTON = f"Не понял кнопку — возьмите новую карточку «{FANT_BUTTON_TEXT}»."
RATE_PROMPT = "Оцените карточку: цифра от 1 до 5 — кнопки ниже."
NO_CARDS_YET = "Новых карточек пока нет — попробуйте позже."

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


def parse_test_user_ids(raw: str | None) -> set[str]:
    """DARDECK_TEST_USER_IDS (csv int, Telegram IDs) -> set строк-идентификаторов.

    Владелец-тест (Task 8): юзер из списка обходит дневной/интервальный гейт
    выдачи. Пустые элементы и не-числа пропускаются (без падения старта бота);
    сравнение — со строковым user.id, поэтому храним как строки.
    """
    if not raw:
        return set()
    ids: set[str] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.add(str(int(part)))
        except ValueError:
            logger.warning("%s: не число %r — элемент пропущен", TEST_USER_IDS_ENV, part)
    return ids


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
        test_users: set[str] | None = None,
    ) -> None:
        self._engine = engine
        self._sender = sender
        self._ugc = ugc  # антифарм голосов + промоушен/жалобы (engine.ugc)
        self._ratings = ratings  # журнал оценок /rate (engine.ratings)
        self._ratings_path = None if ratings_path is None else str(ratings_path)
        self._test_users = test_users or set()  # whitelist: обход интервального гейта
        self._users: dict[str, User] = {}
        self._pairs: dict[str, Pair] = {}  # user_id -> Pair
        self._solo_pairs: dict[str, Pair] = {}  # user_id -> сольная self-пара
        self._pending: list[str] = []  # юзеры в ожидании спаривания
        self._last_card: dict[str, Card] = {}  # pair_id -> последняя выданная карточка
        self._issued: dict[str, list[Card]] = {}  # pair_id -> выданные карточки (по порядку)
        self._issued_to: dict[str, list[Card]] = {}  # user_id -> выданные юзеру карточки
        self._last_issue_at: dict[str, datetime] = {}  # pair_id -> момент последней выдачи


    # -- команды ------------------------------------------------------------

    def start(self, user_id: str, username: str | None) -> str:
        """/start — регистрация юзера."""
        user = self._get_user(user_id, username)
        return (
            "Привет! Dardeck — фанты для вашего вечера.\n"
            "Без пары: /fant (или кнопка «Дать фант») — карточка соло.\n"
            "В паре:\n"
            "1) /pair — стать в пару (оба партнёра должны выполнить)\n"
            "2) /fant или /today — получить карточку дня (обоим)\n"
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
        self._issue(pair, stamped, user_id)
        for u in pair.users:
            self._sender.send(u, self._last_card[pair.id])
        return "Карточка дня отправлена вам обоим. Выполнили — ставьте /done."

    def fant(self, user_id: str, username: str | None = None,
             now: datetime | None = None) -> str:
        """/fant («Дать фант») — выдача: соло-card одиночке, паре — обеим половинками.

        - юзер без пары: синтетическая self-пара Pair(id='solo:<uid>', user_a=u,
          user_b=u) — движок ведёт состояние соло-жизни (no-repeat, учёт) на
          этом PairState; реальная структура пары не затрагивается;
        - юзер в паре: тот же путь, что /today — карточка уходит обоим;
        - антифарм UGC: mark_issued регистрирует фактических получателей;
        - cooldown: free 1/день, кроме whitelist (DARDECK_TEST_USER_IDS).
        """
        user = self._get_user(user_id, username)
        pair = self._pairs.get(user_id)
        if pair is not None:  # парный юзер: /fant — алиас /today
            return self.today(user_id, now)
        solo = self._solo_pair(user)
        stamped = now or datetime.now()
        card = self._issue(solo, stamped, user_id)
        self._sender.send(user, card)
        return "Фант для вас! Выполнили — ставьте /done."

    def show_id(self, user_id: str) -> str:
        """/id — показать свой Telegram ID (всем юзерам, без секретов кроме id)."""
        return f"Ваш Telegram ID: {user_id}"

    def my_cards(self, user_id: str) -> str:
        """«Мои карточки» — последние до 3 выданных юзеру (кратко, с идентификатором)."""
        cards = self._issued_to.get(user_id, [])[-MY_CARDS_LIMIT:]
        if not cards:
            raise NotIssued(f"Пока нет ваших карточек — нажмите «{FANT_BUTTON_TEXT}».")
        lines = []
        for num, card in enumerate(cards, 1):
            text = (
                card.text
                if len(card.text) <= CARD_SNIPPET_MAX
                else card.text[: CARD_SNIPPET_MAX - 1] + "…"
            )
            lines.append(f"{num}. #{card.id} — {text}")
        return "Последние выданные вам карточки:\n" + "\n".join(lines)

    def help_text(self) -> str:
        """/help («Помощь») — шпаргалка по кнопкам и командам."""
        return (
            "Меню: кнопка «" + FANT_BUTTON_TEXT + "» — новая карточка (solo или паре), "
            "«Мои карточки» — последние до 3 выданных, /pair — соединить пару.\n"
            "На сообщении-карточке есть кнопки: ✅ Выполнено, ⭐ Оценить (1..5), "
            "после выполнения — 👍 / 👎 / ⚠ Report.\n"
            "Полный список команд — в ответе на /start. /id — ваш Telegram ID."
        )

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

    def _issue(self, pair: Pair, stamped: datetime, user_id: str) -> Card:
        """Выдача карточки паре (или self-паре): гейт + учёт адаптера + антифарм."""
        self._ensure_daily_gate(pair, stamped, user_id)
        card = self._engine.issue_card(pair)
        self._last_card[pair.id] = card
        self._issued.setdefault(pair.id, []).append(card)
        self._last_issue_at[pair.id] = stamped
        # антифарм UGC: получатели карточки = участники пары (только они голосуют)
        if self._ugc.get(card.id) is None:
            # seed-карточки не создаются через UgcEngine: в реестр их добавляет
            # выдача, чтобы /like /dislike /report по ним резолвились
            self._ugc.state.cards[card.id] = card
        self._ugc.mark_issued(pair, card)
        # журнал «что выдано юзеру» («Мои карточки»): соло-пара это один юзер дважды
        recipients = {u.id: u for u in pair.users}
        for u in recipients.values():
            self._issued_to.setdefault(u.id, []).append(card)
        return card

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

    def last_issued(self, user_id: str) -> Card | None:
        """Последняя выданная юзеру карточка — гейт устаревших inline-кнопок."""
        pair = self._pairs.get(user_id) or self._solo_pairs.get(user_id)
        return None if pair is None else self._last_card.get(pair.id)

    # -- внутреннее -----------------------------------------------------------

    def _get_user(self, user_id: str, username: str | None) -> User:
        user = self._users.get(user_id)
        if user is None:
            user = User(id=user_id, username=username or user_id)
            self._users[user_id] = user
        return user

    def _pair_of(self, user_id: str) -> Pair:
        """Пара юзера: реальная (после /pair) либо соло-пара (после /fant)."""
        pair = self._pairs.get(user_id) or self._solo_pairs.get(user_id)
        if pair is None:
            raise NoPair(
                "Вы не в паре. Возьмите соло-карточку /fant "
                "или начните с /pair — обоим партнёрам нужно вызвать."
            )
        return pair

    def _solo_pair(self, user: User) -> Pair:
        """Синтетическая self-пара соло-жизни: движок ведёт PairState по id.

        Дважды один юзер — карточки no-repeat и учёт live в engine, реальная
        пара (созданная позже) имеет свой pair_id и не смешивается с соло.
        """
        pair = self._solo_pairs.get(user.id)
        if pair is None:
            pair = Pair(id=f"solo:{user.id}", user_a=user, user_b=user)
            self._solo_pairs[user.id] = pair
        return pair

    def _ensure_daily_gate(self, pair: Pair, now: datetime, user_id: str) -> None:
        """Серверный интервал до новой выдачи — политика пары premium-aware:

        - whitelist (DARDECK_TEST_USER_IDS): гейт не применяется — владельцу
          доступен повторный тест выдачи;
        - free-пара (по умолчанию): интервал движка (engine.policy.interval),
          поведение бесплатного режима без изменений;
        - премиум-член пары: resolve_policy даёт premium-режим — интервал
          расслабляется до минимального премиум-лимита (грант премиум-дня,
          например автопромоушеном, позволяет выдавать карточки чаще).
        """
        if user_id in self._test_users:
            return
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


# -- конструкторы клавиатур (маленькие, для ожиданий тестов) -------------------


def callback_of(action: str, card_id: str) -> str:
    """callback_data карточного действия: 'act:<action>:<card_id>' (<=64 байт)."""
    return f"{CB_PREFIX}:{action}:{card_id}"


def main_menu_kb() -> Any:
    """Reply-клавиатура главного меню: [фант] / [карточки, помощь]."""
    from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=FANT_BUTTON_TEXT)],
            [
                KeyboardButton(text=CARDS_BUTTON_TEXT),
                KeyboardButton(text=HELP_BUTTON_TEXT),
            ],
        ],
        resize_keyboard=True,
    )


def card_kb(card_id: str) -> Any:
    """Inline-кнопки свежей карточки: [✅ Выполнено] [⭐ Оценить]."""
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text=DONE_BUTTON_TEXT, callback_data=callback_of(CB_DONE, card_id)),
            InlineKeyboardButton(text=RATE_BUTTON_TEXT, callback_data=callback_of(CB_RATE, card_id)),
        ]]
    )


def rate_kb(card_id: str) -> Any:
    """Второй экран оценки: inline-кнопки 1..5 ('act:rate<n>:<card_id>')."""
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    row = [
        InlineKeyboardButton(text=str(score), callback_data=callback_of(f"rate{score}", card_id))
        for score in range(1, 6)
    ]
    return InlineKeyboardMarkup(inline_keyboard=[row])


def reaction_kb(card_id: str) -> Any:
    """Кнопки после выполнения: [👍] [👎] / [⚠ Report]."""
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=LIKE_BUTTON_TEXT, callback_data=callback_of(CB_LIKE, card_id)),
                InlineKeyboardButton(text=DISLIKE_BUTTON_TEXT, callback_data=callback_of(CB_DISLIKE, card_id)),
            ],
            [InlineKeyboardButton(text=REPORT_BUTTON_TEXT, callback_data=callback_of(CB_REPORT, card_id))],
        ]
    )


def parse_callback(data: str) -> tuple[str, str] | None:
    """'act:done:c1' -> ('done', 'c1'); укороченный/чужой формат -> None."""
    parts = data.split(":")
    if len(parts) == 3 and parts[0] == CB_PREFIX and parts[1] and parts[2]:
        return parts[1], parts[2]
    return None


def score_of(action: str) -> int | None:
    """callback-action 'rate4' -> 4; экранный 'rate' и прочие действия -> None."""
    if not action.startswith(CB_RATE):
        return None
    tail = action[len(CB_RATE):]
    if len(tail) == 1 and tail in "12345":
        return int(tail)
    return None


def _callback_reply(
    service: BotService, data: str, user_id: str
) -> tuple[str, Any | None, bool]:
    """callback 'act:<action>:<card_id>' -> (текст, новая разметка|None, alert?).

    Разметка != None — переставить кнопки сообщения карточки (edit_reply_markup);
    alert=True — answer_callback_query с show_alert (ошибки/пояснения).
    Все ошибки сервиса/движка -> понятный текст (спиннер гасится всегда).
    """
    parsed = parse_callback(data)
    if parsed is None:
        return UNKNOWN_BUTTON, None, True
    action, card_id = parsed
    try:
        score = score_of(action)
        if action == CB_DONE or score is not None:
            # операции по конкретной карточке: кнопка обязана совпасть с последней
            last = service.last_issued(user_id)
            if last is None or last.id != card_id:
                raise BotError(STALE_BUTTON)
            if action == CB_DONE:
                return service.done(user_id), reaction_kb(card_id), False
            return service.rate(user_id, score), reaction_kb(card_id), False
        if action == CB_RATE:  # экран выбора оценки — разметку обновляем
            return RATE_PROMPT, rate_kb(card_id), False
        if action in {CB_LIKE, CB_DISLIKE, CB_REPORT}:
            last = service.last_issued(user_id)
            if last is None or last.id != card_id:
                raise BotError(STALE_BUTTON)
            by_action = {
                CB_LIKE: service.like,
                CB_DISLIKE: service.dislike,
                CB_REPORT: service.report,
            }
            return by_action[action](user_id), None, False
    except BotError as exc:  # NoPair/NotIssued/AlreadyVoted -> friendly
        return str(exc), None, True
    except NoCardAvailable:
        return NO_CARDS_YET, None, True
    return UNKNOWN_BUTTON, None, True


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
            task = loop.create_task(self._send(user.id, card))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        else:
            asyncio.run(self._send(user.id, card))

    async def _send(self, chat_id: str, card: Card) -> None:
        """Доставка в Telegram: сбои не роняют хендлер, но видны в логе.

        Карточка уходит с inline-кнопками [✅ Выполнено][⭐ Оценить] (Task 9).
        """
        try:
            await self._bot.send_message(
                chat_id=chat_id,
                text=format_card(card),
                reply_markup=card_kb(card.id),
            )
        except Exception:
            logger.exception("не удалось доставить карточку в chat %s", chat_id)


def build_router(service: BotService) -> Any:
    """aiogram-роутер: команды + кнопки меню и карточки.

    Команды: /start /pair /fant /today /id /help /done /rate /like /dislike
    /report. Хендлеры тонкие: разбор аргумента команды + перевод ошибок
    (BotError/NoCardAvailable) в понятный текст. aiogram импортируется здесь.
    /today — алиас /fant (обратная совместимость прежних юзеров).

    Кнопки (Task 9): «Мои карточки»/«Помощь» — текстовые кнопки меню; карточки —
    inline-callback 'act:<action>:<card_id>' (спиннер гасится всегда).
    """
    from aiogram import F, Router
    from aiogram.filters import Command, CommandStart

    router = Router(name="dardeck")

    @router.message(CommandStart())
    async def cmd_start(message: Any) -> None:
        reply = service.start(str(message.from_user.id), message.from_user.username)
        await message.answer(reply, reply_markup=main_menu_kb())

    @router.message(Command("pair"))
    async def cmd_pair(message: Any) -> None:
        reply = service.pair(str(message.from_user.id), message.from_user.username)
        await message.answer(reply)

    @router.message(Command("fant"))
    async def cmd_fant(message: Any) -> None:
        username = message.from_user.username
        await _answer(message, lambda uid: service.fant(uid, username))

    @router.message(F.text == FANT_BUTTON_TEXT)  # кнопка клавиатуры — алиас /fant
    async def on_fant_button(message: Any) -> None:
        username = message.from_user.username
        await _answer(message, lambda uid: service.fant(uid, username))

    @router.message(Command("today"))
    async def cmd_today(message: Any) -> None:
        username = message.from_user.username
        await _answer(message, lambda uid: service.fant(uid, username))

    @router.message(Command("id"))
    async def cmd_id(message: Any) -> None:
        await _answer(message, service.show_id)

    @router.message(Command("help"))
    @router.message(F.text == HELP_BUTTON_TEXT)  # кнопка меню «Помощь»
    async def cmd_help(message: Any) -> None:
        await _answer(message, service.help_text)

    @router.message(F.text == CARDS_BUTTON_TEXT)  # кнопка меню «Мои карточки»
    async def on_cards_button(message: Any) -> None:
        await _answer(message, service.my_cards)


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

    @router.callback_query(F.data)  # inline-кнопки карточки ('act:<action>:<id>')
    async def on_callback(cq: Any) -> None:
        """Callback карточки: спиннер гасится всегда (answer_callback_query)."""
        text, markup, alert = _callback_reply(
            service, cq.data or "", str(cq.from_user.id)
        )
        if markup is not None:  # переставить кнопки сообщения карточки
            try:
                await cq.message.edit_reply_markup(reply_markup=markup)
            except Exception:  # сообщение могло устареть — кнопки не критичны
                logger.exception("не удалось переставить кнопки карточки")
        await cq.answer(text, show_alert=alert)

    return router


def _first_arg(message: Any) -> str:
    """Аргумент команды (текст после /cmd) — пустая строка, если нет."""
    args = (message.text or "").split(maxsplit=1)
    return args[1] if len(args) > 1 else ""


async def _answer(message: Any, action: Callable[[str], str]) -> None:
    """Обёртка хендлеров: ошибки сервиса/движка -> текст юзеру, без стеков.

    «Каждое сообщение бота = клавиатура»: обычный ответ уходит с главным меню.
    """
    try:
        reply = action(str(message.from_user.id))
    except BotError as exc:
        await message.answer(str(exc), reply_markup=main_menu_kb())
        return
    except NoCardAvailable:
        await message.answer(NO_CARDS_YET, reply_markup=main_menu_kb())
        return
    await message.answer(reply, reply_markup=main_menu_kb())


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
        test_users=parse_test_user_ids(os.environ.get(TEST_USER_IDS_ENV)),
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
