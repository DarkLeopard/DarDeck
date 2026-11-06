// DarDeck Dark прототип: статические экраны, мок-данные, без бэкенда.
// См. docs/miniapp-research.md (themeParams, safe areas, BackButton, haptics).

"use strict";

(function () {
  const tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;
  const inTelegram = !!tg;

  // --- Telegram WebApp: тема, геометрия, системные кнопки ---

  if (inTelegram) {
    try {
      tg.expand(); // полная высота; viewportStableHeight — для прибитых к низу элементов
    } catch (e) {
      /* не-базовые вызовы могут молча не работать на старых клиентах */
    }
    applyThemeParams(tg.themeParams || {});
    tg.onEvent("themeChanged", () => applyThemeParams(tg.themeParams || {}));
    applySafeAreas();
    if (tg.BackButton) {
      tg.BackButton.onClick(() => show("home"));
    }
  }

  function applyThemeParams(theme) {
    const keys = [
      "bg_color", "secondary_bg_color", "text_color", "hint_color",
      "link_color", "button_color", "button_text_color",
      "header_bg_color", "accent_text_color", "section_bg_color",
      "subtitle_text_color", "destructive_text_color",
    ];
    for (const key of keys) {
      if (theme[key]) {
        document.documentElement.style.setProperty(
          "--tg-theme-" + snakeToCss(key),
          theme[key]
        );
      }
    }
  }

  function snakeToCss(name) {
    // bg_color -> bg-color (CSS-переменные уже --tg-theme-<field>)
    return name.replace(/_/g, "-");
  }

  function applySafeAreas() {
    const root = document.documentElement.style;
    const safe = tg.safeAreaInset;          // 8.0+
    const content = tg.contentSafeAreaInset; // 8.0+
    root.setProperty("--safe-top", (content && content.top || 0) + (safe && safe.top || 0) + "px");
    root.setProperty("--safe-bottom", (safe && safe.bottom || 0) + "px");
  }

  function haptic(kind) {
    if (!inTelegram || !tg.HapticFeedback) return;
    try {
      if (kind === "notify") {
        tg.HapticFeedback.notificationOccurred("success");
      } else {
        tg.HapticFeedback.impactOccurred(kind || "light");
      }
    } catch (e) { /* ignore */ }
  }

  // --- Экраны ---

  const screens = {
    home: document.getElementById("screen-home"),
    card: document.getElementById("screen-card"),
    history: document.getElementById("screen-history"),
  };

  function show(name) {
    for (const [key, el] of Object.entries(screens)) {
      el.hidden = key !== name;
    }
    if (inTelegram && tg.BackButton) {
      if (name === "home") {
        tg.BackButton.hide();
      } else {
        tg.BackButton.show();
      }
    }
    window.scrollTo(0, 0);
  }

  document.addEventListener("click", (ev) => {
    const nav = ev.target.closest("[data-nav]");
    if (!nav || nav.disabled) return;
    haptic("light");
    show(nav.dataset.nav);
  });

  // --- Главное меню + кулдаун-заглушка ---

  let cooldownLeft = 0;
  let cooldownTimer = null;

  function startCooldown(seconds) {
    cooldownLeft = seconds;
    renderCooldown();
    cooldownTimer = setInterval(() => {
      cooldownLeft -= 1;
      renderCooldown();
    }, 1000);
  }

  function renderCooldown() {
    const btn = document.getElementById("btn-draw");
    const box = document.getElementById("cooldown");
    const label = document.getElementById("cooldown-timer");

    if (cooldownLeft <= 0) {
      box.hidden = true;
      btn.hidden = false;
      btn.disabled = false;
      if (cooldownTimer) {
        clearInterval(cooldownTimer);
        cooldownTimer = null;
      }
      return;
    }

    const m = Math.floor(cooldownLeft / 60);
    const s = cooldownLeft % 60;
    label.textContent = m + ":" + String(s).padStart(2, "0");
    box.hidden = false;
    btn.hidden = true;
    btn.disabled = true;
  }

  document.getElementById("btn-draw").addEventListener("click", () => {
    haptic("medium");
    showRandomCard();
    show("card");
    // Заглушка: «взяли фант» → кулдаун до следующего.
    startCooldown(MOCK_COOLDOWN_SECONDS);
  });

  // --- Карточка ---

  const cardTitle = document.getElementById("card-title");
  const cardText = document.getElementById("card-text");
  const cardMeta = document.getElementById("card-meta");
  const cardFeedback = document.getElementById("card-feedback");

  let currentCard = null;

  function showCard(card) {
    currentCard = card;
    cardTitle.textContent = card.title;
    cardText.textContent = card.text;
    cardMeta.textContent =
      "Дека: DarDeck Dark · " + card.category + " · #" +
      card.id.replace(/^seed-|^mock-/, "");
    cardFeedback.textContent = "";
  }

  function showRandomCard() {
    const card = MOCK_CARDS[Math.floor(Math.random() * MOCK_CARDS.length)];
    showCard(card);
  }

  function setFeedback(text) {
    cardFeedback.textContent = text;
  }

  document.getElementById("btn-done").addEventListener("click", () => {
    haptic("notify");
    setFeedback("Отмечено как выполнено (мок)");
  });

  document.getElementById("btn-rate").addEventListener("click", () => {
    haptic("light");
    setFeedback("Оценка появится позже — пока прототип (мок)");
  });

  for (const btn of document.querySelectorAll("[data-react]")) {
    btn.addEventListener("click", () => {
      haptic("light");
      const reactions = {
        like: "👍 Лайк учтён (мок)",
        dislike: "👎 Дизлайк учтён (мок)",
        warn: "⚠ Жалоба отправлена (мок)",
      };
      setFeedback(reactions[btn.dataset.react] || "");
    });
  }

  // --- История (мок-данные) ---

  const historyList = document.getElementById("history-list");

  function renderHistory() {
    historyList.innerHTML = "";
    for (const entry of MOCK_HISTORY) {
      const li = document.createElement("li");

      const date = document.createElement("span");
      date.className = "history-date";
      date.textContent = entry.date;

      // Заглушка: entry.title уже готовый заголовок («Заглушка A»); мок-карты
      // не совпадают с history по id, поэтому отдельного lookup нет.
      const title = document.createElement("span");
      title.className = "history-title";
      title.textContent = entry.title;

      const verdict = document.createElement("span");
      verdict.className = "history-rating";
      verdict.textContent = entry.verdict;

      li.append(date, title, verdict);
      historyList.append(li);
    }
  }

  // --- Инициализация ---

  if (!inTelegram) {
    // Обычный браузер: дескриптор «прототип», dev-заглушка вместо initData.
    document.getElementById("env-badge").hidden = false;
  }

  renderCooldown();
  renderHistory();
  show("home");
})();
