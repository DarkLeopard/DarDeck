# Telegram Mini Apps: исследование для DarDeck

Источник: https://core.telegram.org/bots/webapps (свежая редакция, страница имеет machine-readable версию `/bots/webapps.md`; последние изменения — июнь 2026, Bot API 10.1). Дополнительно: core.telegram.org/bots/api (web_app-кнопки, MenuButtonWebApp, answerWebAppQuery).

Наш бот: [@dardeckdark_bot](https://t.me/dardeckdark_bot), движок — `engine/`, бот — `bots/telegram/bot.py` (aiogram 3), контент — фанты для пар, 7 дек.

---

## 1. Способы открыть мини-апп

Официально поддерживается **7 способов запуска**. Для DarDeck релевантны первые четыре:

| Способ | Как настроить | Что важно |
|---|---|---|
| **Main Mini App** (кнопка «Launch App» на профиле бота) | @BotFather → /mybots → Bot Settings → **Configure Mini App** → Enable; там же кастомизация экрана загрузки (иконка, цвета light/dark) | Открывается полной высотой по умолчанию; `mode=compact` в ссылке даёт половину экрана. После включения бот получает «Launch app» на профиле, превью-медиа и попадает в таб *Apps* поиска |
| **Inline-кнопка `web_app`** | `InlineKeyboardButton(text, web_app=Url(url))` в aiogram | Главный вариант для DarDeck: кнопка «Дать фант 🎴» в клавиатуре. Даёт `query_id` (answerWebAppQuery — отправить сообщение от имени пользователя) |
| **Reply-клавиатура `web_app`** (KeyboardButtonWebApp) | обычная reply-клавиатура с web_app-кнопкой | Только метод `Telegram.WebApp.sendData(data)` (≤4096 байт) отправляет боту service message (`web_app_data` в Message) — мини-апп закрывается. HTTP-запросы на свой сервер тоже можно, но sendData недоступен из inline-мода |
| **Menu Button** | `setChatMenuButton(menu_button=MenuButtonWebApp(text, web_app))` из кода бота **или** BotFather `/setmenubutton` | Постоянная ⚡-кнопка внизу чата. Можно менять per-user (например, язык). Поведение идентично inline-кнопке |
| Direct link | `https://t.me/dardeckdark_bot?startapp=...` или `t.me/bot/app_short_name` (нужно задать Main Mini App + short_name в BotFather) | `startapp` → `start_param` (и GET-параметр `tgWebAppStartParam`); с Bot API 7.6 открывается full-screen по умолчанию |
| Inline mode | `answerInlineQuery(button=...)` | «Switch to Mini App». Не подходит нам |
| Attachment menu (скрепка) | `/setattach` в BotFather, **только для крупных рекламодателей** (на проде) | Не наш вариант |

Рекомендация для DarDeck: **inline-кнопка на `/start`** («🎴 Дать фант»), **MenuButtonWebApp** через `setChatMenuButton`, и Main Mini App в BotFather как публичная точка входа. Прямой линк `?startapp=deck:<id>` удобно использовать для «расшарить деку партнёру».

Ключевой SDK-скрипт (обязателен первым в `<head>`):

```html
<script src="https://telegram.org/js/telegram-web-app.js?64"></script>
```

После подключения доступен `window.Telegram.WebApp` (initData, themeParams, BackButton, BottomButton/MainButton, HapticFeedback, CloudStorage, DeviceStorage/SecureStorage, expand(), isVersionAtLeast() и др.).

## 2. Что даёт initData

`Telegram.WebApp.initData` — **query string** (сырые данные), которые мини-апп обязан передать на бэкенд и валидировать там. Поля:

- `user` — JSON: `id, first_name, last_name, username, language_code, photo_url (с Bot API 8.0 для всех), is_premium, added_to_attachment_menu, allows_write_to_pm`
- `auth_date` — unix-время получения
- `query_id` — идентификатор сессии (есть при запуске из inline-кнопки/menu/attachment); нужен боту для `answerWebAppQuery`
- `chat` + `chat_type` + `chat_instance` — контекст чата (для запуска из чатa/direct-link; chat_instance — общий ключ для нескольких участников)
- `start_param` — из `?startapp=`
- `signature` — Ed25519-подпись (для third-party валидации без токена бота; прод-ключ Telegram: `e7bf03a2fa4602af4580703d88dda5bb59f32ed8b02a56c187fe7d34caed242d`)

**initDataUnsafe нельзя доверять** — только initData после проверки на сервере.

## 3. Валидация initData (псевдокод Python, без Django)

Алгоритм из официльных доков:

1. `secret_key = HMAC_SHA256(key="WebAppData", msg=bot_token)`
2. `data_check_string` = все поля кроме `hash`, отсортированные по ключу, `key=<value>`, разделитель `\n` (0x0A). Поля **не** url-decode-ить повторно: берём значения в том виде, как пришли в query string.
3. `calculated = hex(HMAC_SHA256(key=secret_key, msg=data_check_string))`; сравнить с `hash` (constant-time compare).
4. Дополнительно проверить `auth_date` (свежесть, например ≤ 24 ч) и, при желании, IP/платформу.

Python (stdlib only) псевдокод:

```python
import hmac, hashlib, time, urllib.parse, json

def validate_init_data(init_data: str, bot_token: str, max_age: int = 24*3600) -> dict:
    """
    init_data — сырая строка из Telegram.WebApp.initData
    (приходит от фронтенда в заголовке или теле POST-запроса).
    Бросает ValueError, если данные невалидны. Возвращает распарсенные поля.
    """
    pairs = urllib.parse.parse_qsl(init_data, keep_blank_values=True)
    data = dict(pairs)

    received_hash = data.pop("hash", None)
    if not received_hash:
        raise ValueError("no hash")
    # поля с url-энкодом НЕ разкодируем обратно — сверяем как есть
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs) if k != "hash")

    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, received_hash):
        raise ValueError("bad signature")

    auth_date = int(data.get("auth_date", "0"))
    if time.time() - auth_date > max_age:
        raise ValueError("stale initData")

    user = json.loads(data["user"]) if "user" in data else None
    return {"user": user, "query_id": data.get("query_id"),
            "start_param": data.get("start_param"),
            "auth_date": auth_date, "raw": data}
```

Нюансы, на которые часто наступают:

- **Порядок**: сортировка по ключу (`sorted`), разделитель `\n` (не `&`).
- **Не декодировать** значения перед подписанием — `data_check_string` строится из исходных (по-прежнему url-encoded) значений, иначе подпись не сойдётся. `parse_qsl` декодирует, поэтому безопаснее парсить строку руками (`split('&')` → `split('=', 1)`) и собрать `key=value` без изменений.
- Двойной HMAC: ключ = `HMAC_SHA256("WebAppData", bot_token)` — перепутать порядок аргументов легко, порядок: **сообщение = bot_token, ключ = "WebAppData"** для секрета; затем **сообщение = data_check_string, ключ = secret_key**.
- Проверка `auth_date` обязательна для защиты от replay: перехваченный `initData` можно подсовывать бесконечно, если не проверять свежесть.
- Идентификация юзера: доверяем **только** `user.id` после валидации. Никогда не верить `user.id`, пришедшему в обычном JSON/tg-запросе со стороны клиента.

Третья сторона может валидировать через Ed25519-подпись (`signature`) без знания токена бота — ДарДеку пока не нужно, но отмечаем как путь, если когда-нибудь появятся внешние интеграции.

## 4. ThemeParams и внешний вид

`Telegram.WebApp.themeParams` и `colorScheme` приходят в реальном времени (событие `themeChanged`). Ключевые поля: `bg_color`, `secondary_bg_color` (+ `bottom_bar_bg_color` с 7.10), `text_color`, `hint_color`, `link_color`, `button_color`, `button_text_color`, `header_bg_color`, `accent_text_color`, `section_bg_color`, `section_header_text_color`, `subtitle_text_color`, `destructive_text_color`, `section_separator_color` (7.0+).

- CSS-переменные из коробки: `--tg-color-scheme`, `--tg-theme-bg-color`, `--tg-theme-secondary-bg-color`, `--tg-theme-text-color`, `--tg-theme-button-color`, `--tg-theme-hint-color` и т.п. — рекомендуем строить тему на них, а не на захардкоженных цветах.
- `setHeaderColor`, `setBackgroundColor`, `setBottomBarColor` (7.10+) — привести шапку/низ к тёмному стилю DarDeck.
- Геометрия: `expand()` — развернуть на всю высоту; `viewportStableHeight` (не `viewportHeight`) для прибитых к низу элементов; `safeAreaInset` / `contentSafeAreaInset` (8.0+) для notch.
- `BackButton.show()/onClick()` — навигация между экранами карточек; `HapticFeedback.impactOccurred('light')` на выбор фанта/лайк.
- Design guidelines: mobile-first, 60fps, доступность (labels), учитывать performance class Android из User-Agent.

## 5. Ограничения и требования деплоя

- **Только HTTPS**, порты **443 (или 80/88/8443)**; самоподписанный сертификат не подойдёт. Обычное требование Bot API для web_app URL.
- Домен мини-аппа надо указать в BotFather (Configure Mini App / Menu Button / кнопки клавиатур) — Telegram сверяет домен при открытии.
- URL открывается внутри webview Telegram; данные в Telegram передаются с заголовками браузера webview — на бэкенде это обычный HTTP-клиент.
- Cookie/localStorage в webview работают не на всех платформах стабильно → **авторизацию держим на валидном initData**, а не на сессиях-куках. Для локальных данных на устройстве — `CloudStorage`, `DeviceStorage`, `SecureStorage` (9.0+).
- `sendData` доступен **только** для запуска через reply-кнопку (Keyboard button); из inline-мода — `answerWebAppQuery`.
- Версии Bot API: у пользователя `WebApp.version` — реальная версия его клиента. Все не-базовые вызовы оборачивать в `WebApp.isVersionAtLeast('7.10')` etc., иначе молча ничего не произойдёт.

## 6. Инфраструктура разработки и деплоя

Варианты для прод:

- Статические файлы (SPA) на любом https-хосте: nginx + certbot, Vercel, Netlify, Cloudflare Pages, GitHub Pages (последний — удобно, т.к. репо на GitHub; кастомный домен не нужен). API-запросы на бэкенд ДарДека (`POST /api/...` с initData в заголовке `X-Telegram-Init-Data` → CORS: разрешить origin мини-аппа).
- Для dev: **cloudflared tunnel** (`cloudflared tunnel --url http://localhost:8000`) или **ngrok** (`ngrok http 8000`) — даёт публичный https на локальной машине, URL подставляется в web_app-кнопки и BotFather.
- Локальная отладка без Telegram: `Telegram.WebApp.version` будет равным 6.x-заглушке, initData пустой — добавлять dev-заглушку с тестовым юзером (только в dev-сборке).
- Отладка внутри Telegram: `WebApp.openTelegramLink('https://t.me/webbot_dev_bot')` не нужен — на десктопе достаточно `Ctrl+Shift+I` в Telegram Desktop; для iOS — Safari-инспектор (поддержка появилась с 8.0).

## 7. Связка юзера мини-аппа с движком DarDeck

Поток аутентификации:

1. Фронт мини-аппа берёт `Telegram.WebApp.initData` и шлёт его на API движка в каждом запросе (заголовок `X-Telegram-Init-Data`).
2. Сервер валидирует (см. §2) → получает `user.id` → **это и есть тг-id юзера**, по которому бот уже знает пару/прогресс/кулдауны.
3. Движок (`engine/models.py`) оперирует тем же ключом `tg_user_id`, что и bot.py — отдельной таблицы сессий не нужно: мини-апп и бот живут на одном аккаунте Telegram.
4. `start_param` — канал параметров запуска: например `?startapp=deck:fanty7` — открыть конкретную деку; `?startapp=pick` после лайка — сразу карточка.
5. Обратная связь «из мини-аппа в бот»: результат действия (фант выбран, лайк поставлен) можно отправить через `answerWebAppQuery` (inline-сообщение в чат, где открыт ап) — но в 1:1-чате пары это дублирует чат-историю; проще пусть серверная часть пишет результат в `engine` (БД), а бот при следующем обновлении клавиатур отражает состояние. `sendData` рассмотрен только если открывать ап из reply-клавиатуры.

Итог: **auth = initData → user.id → engine**, никакого дублирующего логина/пароля.

## 8. UI-скетч экранов

Основано на паттернах koloda (single-page, список → карточка → ход, без build-шага) и Telegram design guidelines.

### 8.1 Главный экран — «Дать фант»
```
┌───────────────────────────┐
│ ⚙ DarDeck        [👤 ты]  │  ← header (setHeaderColor)
│                           │
│  Что сегодня?             │
│  [🎴 Дать фант]          ← BottomButton (основная кнопка, оранж)
│                           │
│  Дека: Фанты для пар      │   (selector, если дек >1; по умолчанию текущая)
│  Кнопка-напоминание       │   «История», «Оценить» — обычные элементы
│  · История                │
│  · Настройки              │
└───────────────────────────┘
```
Нажатие → haptic → экран Карточки. Кнопка «Дать фант» дублируется в боте — на `/start` остаётся inline `web_app`-кнопка, чтобы попасть сюда из чата одним тапом.

### 8.2 Карточка
```
┌───────────────────────────┐
│ ← назад        [⭐ Избр] │  ← BackButton Telegram
│                           │
│      ✦ ФАНТ ✦            │
│                           │
│   «Устрой партнёру       │
│    маленький сюрприз      │
│    прямо сейчас…»         │
│                           │
│  Дека: Фанты для пар · #42│
│                           │
│ [✅ Сделал]  [↻ Другой]  │
└───────────────────────────┘
```
«Другой» — перераздача с учётом уже выпадавших (логика engine). «Сделал» → оценка.

### 8.3 Оценка / лайки
```
┌───────────────────────────┐
│ ← назад                   │
│  Как оно было?            │
│                           │
│   💔    😐    🔥    💘   │  ← 4 варианта оценки (или 1–5 звёзд)
│                           │
│ [Пропустить]              │
└───────────────────────────┘
```
После оценки — «Партнёр увидит лайк в боте» (уведомление приходит из бота). Rating сохраняется в `engine.ratings`.

### 8.4 История
```
┌───────────────────────────┐
│ ← назад        История    │
│  за 7 дней        ▾       │
│  · 12.05 ✅ «Сюрприз» 🔥 │
│  · 11.05 ⏭ Пропущено     │
│  · 10.05 ✅ «Массаж» 💘  │
│  …                        │
│ [В боте подробная        ] │  ← ссылка на чат-историю бота
└───────────────────────────┘
```
Плоский список, без инфинити-скролла сложности.

### 8.5 Кулдаун-заглушка
```
┌───────────────────────────┐
│      ⏳ Ещё немного        │
│                           │
│   Новый фант через        │
│     🔔 0:47               │
│                           │
│ [Понятно]  [Кулдаун-таймер│
│              → история]   │
└───────────────────────────┘
```
Таймер крутится локально (не дёргает сервер), кулдаун-время приходит с сервера вместе с ответом «ты в кулдауне».

## 9. Что оставить в боте, что вынести в мини-апп

**Оставить в боте (chat-флоу):**
- `/start`, онбординг, выбор деки (текстовые inline-кнопки), правила.
- `web_app`-кнопка «🎴 Дать фант» (главная точка входа).
- Уведомления партнёру («Партнёр дал фант — открой»), напоминания по кулдауну, лайки/«спасибо» от второго партнёра как обычные сообщения.
- Настройки (язык, напоминания) можно дать в боте — меньше трения, чем идти в ап.

**Вынести в мини-апп:**
- Сам процесс «дать фант» (карточка, «Сделал», «Другой») — тут важна красивая подача, анимация, таймер.
- Оценка/лайки (нужен интерактив, который кнопками чата выглядит бедно).
- История с визуальной лентой.
- Кулдаун-заглушка с живым таймером.
- Прочее «экранное» (главная, статистика пары).

Принцип: бот = точка входа + уведомления + простые текстовые команды. Мини-апп = всё, что требует визуального состояния и анимаций. Это же позволяет не вешать кулдауны на чат-кнопки (которые бот не может обновлять по расписанию) — таймер внутри ап живёт локально.

---

*Ссылки:*
- https://core.telegram.org/bots/webapps (и `/bots/webapps.md`)
- https://core.telegram.org/bots/api — `KeyboardButtonWebApp`, `MenuButtonWebApp`, `answerWebAppQuery`, `setChatMenuButton`, `WebAppInitData`
- https://telegram.org/blog/privacy-discussions-web-bots — seamless auth
