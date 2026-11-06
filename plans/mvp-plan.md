# Plan — DarDeck MVP (branch sdd/mvp-plan, base 2b1c...=8521e48)

Test command for all tasks: `docker compose run --rm app pytest -q` (после T1; до T1 — pytest доступен в контейнере из образа python:3.12-slim).

## Task 1 — Docker-среда и каркас тестов
- Files: Dockerfile (python:3.12-slim + deps: pytest, aiogram>=3; non-root), docker-compose.yml (service `app`, env_file=.env optional, volumes source), pyproject.toml (project dardeck, deps minimal, pytest config), engine/__init__.py, tests/test_smoke.py (import engine ok).
- Constraint: docker build+pytest must run fully inside container; .env и packs/ не копируются в образ (dockerignore).
- DoD: `docker compose build` ok; `docker compose run --rm app pytest -q` зелёный.

## Task 2 — Ядро: домены, политика выдачи, issue_card
- Files: engine/models.py (User, Pair, Card, dataclass/enum), engine/policy.py (DeliveryPolicy, premium/free-логика), engine/engine.py (issue_card(pair): no-repeat по тексту, is_sponsored cap 1/7, учёт issued/completed), tests/test_policy.py, tests/test_issue.py (в т.ч. anti-repeat, sponsored-cap).
- Depends: T1. Interface pinned: CardSource.fetch_candidates, RulePlugin.select (пустые протоколы в engine/interfaces.py).

## Task 3 — Адаптеры: SQLite CardSource + RulePlugin'ы
- Files: adapters/db/sqlite.py (схема: cards, pairs_history; fetch_candidates), adapters/rules/random_rule.py, adapters/rules/rotation_rule.py, tests/test_adapters.py.
- Depends: T2 (protocols). Constraint: schema создаётся адаптером, не движком; путь к БД — аргумент конструктора.

## Task 4 — Telegram-бот поверх Sender
- Files: bots/telegram/bot.py (aiogram v3, dardeck bot with token via env, dry-run/log Sender fallback), bots/sender.py (Sender ABC + MockSender for tests), tests/test_bot.py (интеграция с движком на MockSender, без сети).
- Depends: T2, T3.

## Task 5 — UGC: like/dislike, автопул, премиум-день
- Files: engine/ugc.py (create_card, like/dislike с проверкой получателя, promo-логика 10/0, report 3 → retired, grant premium day), tests/test_ugc.py (антифарм, порог, ретир).
- Depends: T2 (+T3 для хранения статусов).

## Task 6 — Отзывы и чистка
- Files: engine/ratings.py (rate + review, среднее), tools/cleanup.py (ревью-список: avg<0 при ≥5 оцен; удаление только с --confirm; в docker-сервисе run), tests/test_ratings.py.
- Depends: T5.
