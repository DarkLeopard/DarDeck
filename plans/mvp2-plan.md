# Plan — MVP-2 wave (branch: main, autonomous; ledger v2)

Test command: docker compose run --rm app pytest -q

## Task 8 — Solo-режим, владелец-тест, /fant
Semantics (binding):
- Любой юзер без пары: команда/кнопка «Дать фант» → движок выдаёт ему карточку СОЛО. Реализация: синтетическая self-pair Pair(id='solo:<uid>', user_a=u, user_b=u) на юзера; состояние соло-жизни живёт на этом же PairState (no-repeat, учёт) — пара не ломает соло-историю.
- /pair остаётся (обоюдные вызовы, матч), но НЕ обязателен; /today переименовать по смыслу: главной точкой выдачи становится «Дать фант» (соло). После пары «Дать фант» выдаёт обоим партнёрам по карточке (как раньше /today для обоих).
- Тестовый доступ владельца: env DARDECK_TEST_USER_IDS (csv инт Telegram ID, опционально). Юзер из списка: обходит дневной/интервальный гейт (можно выдавать бесконечно), дополнительно /id показывает свой Telegram ID (доступна всегда всем — простая команда, не секрет).
- Антифарм UGC сохраняется: лайки только получателям (соло-выдача регистрирует получателя как прежде через mark_issued).
- Cooldown-правило: соло-выдача подчиняется политике как пара (free 1/день), кроме whitelist-юзеров.
Files: bots/telegram/bot.py (solo-path, /fant, /id, whitelist-gate), engine/* без изменений (синтетика на уровне бота), tests/test_bot.py + new tests.

## Task 9 — Кнопки (inline UI)
Semantics (binding):
- Каждое сообщение бота = клавиатура. Главное меню (reply-keyboard): [🎲 Дать фант] [Мои карточки] [Помощь].
- Карточка выдаётся сообщением с inline-кнопками: [✅ Выполнено] [⭐ Оценить] (1..5 inline) и после выполнения [👍] [👎] [⚠ Report].
- callback_data по паттерну 'act:<action>:<card_id>'; обработка тех же BotService-методов; дубликаты (вторая /done) → дружелюбный ответ, клавиатура апдейтится (edit_message).
- /start приветствие с reply-keyboard.
Files: bots/telegram/bot.py, tests/test_bot.py (callback-флоу через aiogram callback mock).

## Task 10 — Mini App: исследование + каркас (design/docs, без боевого деплоя)
- docs/miniapp-research.md: Telegram Mini App (web_app button, initData validation HmacSHA256, themeParams, safe areas, JWT-подход), как валидировать юзера в нашем движке, требования https и пути деплоя (cloudflared tunnel / будущий VPS), UI-скетч экранов (Фант, Колода, История, Оценка).
- Прототип: web/ стейджинг (один статический HTML+JS, без бэкенда, локальный http-сервер для разработки, данные через мок) — деплой НЕ выполнять.
Files: docs/miniapp-research.md, web/index.html(+js/css), tests не нужны (статика), но pip-линтер/билд не затрагивать.

## Task 11 — Интеграционная добивка волны (после T8-T10)
- По факту находок финального ревью волны 2.
