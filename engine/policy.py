"""Политика выдачи: DeliveryPolicy и free/premium-логика.

Сервер решает: free = серверный интервал (по умолчанию 1 карточка/день),
юзер частоту не меняет; premium = юзер управляет частотой/количеством
в заданных лимитах.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .models import User

# free: серверный интервал по умолчанию (1 карточка в день)
DEFAULT_FREE_INTERVAL = timedelta(days=1)

# premium: лимиты, в которых юзер может управлять выставлением
PREMIUM_MIN_INTERVAL = timedelta(hours=1)
PREMIUM_MAX_CARDS_PER_CYCLE = 10
PREMIUM_MAX_INTERVAL = timedelta(days=7)


@dataclass(frozen=True)
class DeliveryPolicy:
    """Решение сервера о режиме выдачи для пары."""

    interval: timedelta = DEFAULT_FREE_INTERVAL
    cards_per_cycle: int = 1
    allow_user_control: bool = False


def free_policy() -> DeliveryPolicy:
    """Серверный режим: интервал не меняется юзером."""
    return DeliveryPolicy(interval=DEFAULT_FREE_INTERVAL, cards_per_cycle=1, allow_user_control=False)


def premium_policy(
    interval: timedelta | None = None,
    cards_per_cycle: int | None = None,
) -> DeliveryPolicy:
    """Premium-режим: юзер управляет в заданных лимитах (values клампятся)."""
    resolved_interval = interval if interval is not None else DEFAULT_FREE_INTERVAL
    resolved_interval = max(PREMIUM_MIN_INTERVAL, min(PREMIUM_MAX_INTERVAL, resolved_interval))
    resolved_cycles = 1 if cards_per_cycle is None else cards_per_cycle
    resolved_cycles = max(1, min(PREMIUM_MAX_CARDS_PER_CYCLE, resolved_cycles))
    return DeliveryPolicy(
        interval=resolved_interval,
        cards_per_cycle=resolved_cycles,
        allow_user_control=True,
    )


def is_premium_active(user: User, now: datetime | None = None) -> bool:
    """Premium действует, пока стоит флаг и не истёк premium_until."""
    if not user.is_premium:
        return False
    if user.premium_until is None:
        return True
    return user.premium_until > (now or datetime.now())


def resolve_policy(user: User, now: datetime | None = None) -> DeliveryPolicy:
    """Политика выдачи для юзера: premium → контролируемая, иначе серверная."""
    if is_premium_active(user, now):
        return premium_policy()
    return free_policy()
