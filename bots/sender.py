"""Интерфейс доставки карточки: Sender.send(user, card) (спецификация Dardeck).

- Sender — ABC-контракт доставки;
- MockSender — тестовая реализация (запоминает, ничего не отправляет);
- LogSender — dry-run: пишет в лог вместо отправки (бот без токена).

Отправки адресуются юзеру (User), форматирование текста карточки —
телефонам бота (Telegram-слой), не движку.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

from engine.models import Card, User


class Sender(ABC):
    """Контракт доставки: send(user, card)."""

    @abstractmethod
    def send(self, user: User, card: Card) -> None:
        """Доставить карточку юзеру."""
        ...


class MockSender(Sender):
    """Тестовая реализация: копит (User, Card) в sent, сетевой ввод-вывод не трогает."""

    def __init__(self) -> None:
        self.sent: list[tuple[User, Card]] = []

    def send(self, user: User, card: Card) -> None:
        self.sent.append((user, card))


class LogSender(Sender):
    """Dry-run Sender: карточка пишется в лог (и в records), отправки нет.

    records — для тестов и наблюдаемости; отправкой это не считается.
    """

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._log = logger if logger is not None else logging.getLogger("dardeck.bots")
        self.records: list[tuple[str, str]] = []  # (user.id, card.id) — dry-run вместо отправки

    def send(self, user: User, card: Card) -> None:
        self.records.append((user.id, card.id))
        self._log.info(
            "dry-run send: user=%s card=%s text=%r",
            user.id,
            card.id,
            card.text,
        )
