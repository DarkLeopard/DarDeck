"""Тесты политики выдачи: free/premium-логика DeliveryPolicy."""

from datetime import datetime, timedelta

from engine.models import User
from engine.policy import (
    DEFAULT_FREE_INTERVAL,
    PREMIUM_MAX_CARDS_PER_CYCLE,
    PREMIUM_MIN_INTERVAL,
    DeliveryPolicy,
    free_policy,
    is_premium_active,
    premium_policy,
    resolve_policy,
)


def test_free_policy_is_server_controlled():
    policy = free_policy()
    assert policy.interval == DEFAULT_FREE_INTERVAL == timedelta(days=1)
    assert policy.cards_per_cycle == 1
    assert policy.allow_user_control is False


def test_free_user_cannot_change_interval():
    user = User(id="u1", username="leo")
    policy = resolve_policy(user)
    assert isinstance(policy, DeliveryPolicy)
    assert policy.allow_user_control is False
    assert policy == free_policy()


def test_premium_user_gets_control():
    user = User(id="u1", username="leo", is_premium=True,
                premium_until=datetime.now() + timedelta(days=30))
    policy = resolve_policy(user)
    assert policy.allow_user_control is True
    assert policy == premium_policy()


def test_expired_premium_falls_back_to_free():
    user = User(id="u1", username="leo", is_premium=True,
                premium_until=datetime.now() - timedelta(days=1))
    assert is_premium_active(user) is False
    assert resolve_policy(user) == free_policy()


def test_premium_without_expiry_is_active():
    user = User(id="u1", username="leo", is_premium=True)
    assert is_premium_active(user) is True


def test_premium_policy_clamps_interval_to_limits():
    too_fast = premium_policy(interval=timedelta(minutes=1))
    assert too_fast.interval == PREMIUM_MIN_INTERVAL
    assert too_fast.interval >= PREMIUM_MIN_INTERVAL

    too_slow = premium_policy(interval=timedelta(days=30))
    assert too_slow.interval <= timedelta(days=7)


def test_premium_policy_clamps_cards_per_cycle():
    assert premium_policy(cards_per_cycle=100).cards_per_cycle == PREMIUM_MAX_CARDS_PER_CYCLE
    assert premium_policy(cards_per_cycle=0).cards_per_cycle == 1
    in_limits = premium_policy(cards_per_cycle=3, interval=timedelta(hours=2))
    assert in_limits.cards_per_cycle == 3
    assert in_limits.interval == timedelta(hours=2)


def test_delivery_policy_is_frozen():
    policy = DeliveryPolicy()
    try:
        policy.interval = timedelta(hours=1)  # type: ignore[misc]
    except Exception:
        return
    raise AssertionError("DeliveryPolicy должен быть неизменяемым (решает сервер)")
