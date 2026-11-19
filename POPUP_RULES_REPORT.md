# Звіт про правила автоматичного проклікування (3ds Max 2025)

Цей документ описує правила з [config/rules.yaml](file:///Z:/config/rules.yaml), налаштовані під час інтерактивного першого запуску **Autodesk 3ds Max 2025**.

> **Як працює `watcher.py`:** правила перевіряються **зверху вниз** (спрацьовує перше правило, яке підійшло). Для `click_control` спочатку шукається елемент за `automation_id`, потім за точною назвою (`control_names`), а `click_relative` клікає за відносними координатами вікна (`0.0..1.0`).

---

## Зафіксовані правила

### 1. Вікно конфіденційності (Privacy / Data Collection Consent)
- **Коли з'являється:** Перший запуск `3dsmax.exe`, вікно з текстом `"We care about your privacy"`.
- **Як розпізнається:**
  - Процес містить: `3dsmax`
  - Заголовок вікна: `^3dsmax$` (точний збіг `'3dsmax'`)
  - Текст вікна містить: `"We care about your privacy"` або `"Required data collection"`
- **Яка дія виконується:** `click_control` → натискає кнопку **`OK`** (`automation_id: "1002"`).
- **Еталонний дамп/скріншот:** [popup_20261114_131731_1717a78346ef.json](file:///Z:/logs/unknown_popups/popup_20261114_131731_1717a78346ef.json) / [PNG](file:///Z:/logs/unknown_popups/popup_20261114_131731_1717a78346ef.png)

---

### 2. Вибір мережевої ліцензії (Autodesk Licensing — Let's Get Started)
- **Коли з'являється:** Після натискання `OK` у вікні Privacy відкривається вікно ліцензування Autodesk (*Let's Get Started*).
- **Як розпізнається:**
  - Процес містить: `AdskLicensingAgent`
  - Заголовок вікна: `WebView2 WebBrowser`
  - Текст вікна містить: `"Untitled"` або `"Clic LGS"`
  - *(Примітка: це правило стоїть **нижче** за правило №3, щоб не перехоплювати наступний екран `"Specify a license server"`.)*
- **Яка дія виконується:** `click_relative` → клік у відносні координати вікна `(rel_x: 0.66, rel_y: 0.76)`, що відповідає кнопці **`Select`** у блоці **Use a network license**.
  - *Чому `click_relative`:* на цьому екрані WebView2 не віддає внутрішні HTML-кнопки через Windows UI Automation, тому використовується клік за відносною позицією всередині вікна (працює незалежно від того, де вікно відкриється на екрані).
- **Еталонний дамп/скріншот:** [popup_20261114_132348_c2b5b5cc33a3.json](file:///Z:/logs/unknown_popups/popup_20261114_132348_c2b5b5cc33a3.json) / [PNG](file:///Z:/logs/unknown_popups/popup_20261114_132348_c2b5b5cc33a3.png)

---

### 3. Закриття вікна сервера ліцензій (Autodesk Licensing — Specify a license server)
- **Коли з'являється:** Одразу після натискання **Select** під **Use a network license**.
- **Як розпізнається:**
  - Процес містить: `AdskLicensingAgent`
  - Заголовок вікна: `WebView2 WebBrowser`
  - Текст вікна містить: `"Specify a license server"` або `"Server Name"`
  - *(Стоїть у `rules.yaml` **перед** правилом №2, оскільки має більш специфічний текст.)*
- **Яка дія виконується:** `close` → закриває вікно (`WinClose` через AutoHotkey v2), не натискаючи `Done`.
- **Еталонний дамп/скріншот:** [popup_20261114_132619_202d1167243c.json](file:///Z:/logs/unknown_popups/popup_20261114_132619_202d1167243c.json) / [PNG](file:///Z:/logs/unknown_popups/popup_20261114_132619_202d1167243c.png)

---

### 4. Помилка ліцензії на старті (`License Startup Error`)
- **Коли з'являється:** Якщо вікно вибору ліцензії було закрито без підключеної ліцензії/сервера.
- **Як розпізнається:**
  - Процес містить: `3dsmax`
  - Заголовок вікна: `^License Startup Error$`
  - Текст вікна містить: `"problem with the software license"`
- **Яка дія виконується:** `click_control` → натискає кнопку **`OK`**.

---

### 5. Запит про кольори / тему на старті (`Color Management / Theme Prompt`) *(перевірено наживо)*
- **Коли з'являється:** Після проходження ліцензування (або якщо вікно ADSK не з'являлося), 3ds Max показує вікно `'3dsmax'` з текстом `"Heads Up! | The 3ds Max default Color Management mode has changed."`.
- **Як розпізнається:**
  - Процес містить: `3dsmax`
  - Заголовок вікна: `^3dsmax$`
  - Текст вікна містить: `"Heads Up!"`, `"Color Management"` або `"Don't show again"`
- **Яка дія виконується:** `sequence` (послідовність із 2 кроків через нативний UIA без прив'язки до розмірів/координат):
  1. `click_control` → `TogglePattern` ставить галочку **`Don't show again`** (пауза `0.3s`).
  2. `click_control` → `InvokePattern` натискає кнопку **`Continue`**.
- **Результат перевірки:** Галочка успішно встановлена (`0 -> 1`), кнопка `Continue` натиснута, відкрилося головне вікно **`Untitled - Autodesk 3ds Max 2025`**, а скрипт [verify_install.ms](file:///Z:/max_scripts/verify_install.ms) створив маркер `%TEMP%\max_2025_ready.txt`.

---

### 6. Екран привітання (`Welcome to 3ds Max`)
- **Коли з'являється:** Всередині головного вікна `Untitled - Autodesk 3ds Max 2025` одразу після завантаження.
- **Як розпізнається:**
  - Процес містить: `3dsmax`
  - Текст вікна містить: `"Welcome to 3ds Max"`
  - Має прапорець `run_before_ignore: true`, тому виконується **до** завершення watcher на головному вікні.
- **Яка дія виконується:** `sequence` (послідовність із 2 кроків через нативний UIA):
  1. `click_control` → `TogglePattern` (`state: 0`) **знімає галочку** **`Show this Welcome Screen at startup`** (`automation_id: "WelcomeScreenDialog.mShowWelcomeScreenBox"`, пауза `0.3s`).
  2. `click_control` → `InvokePattern` натискає кнопку **`x`** (`automation_id: "WelcomeScreenDialog.closeButton"`).

---

### 7. Вікно повідомлень безпеки (`Security / QMessageWidget`)
- **Коли з'являється:** Всередині головного вікна 3ds Max 2025 (панель із `Open on blocked commands`, `Auto-Scroll`, `Clear`, `Close`).
- **Як розпізнається:**
  - Процес містить: `3dsmax`
  - Наявність контрола з точним `automation_id: "QMessageWidget.closePushButton"` (без fallback на загальне ім'я `"Close"`, щоб випадково не закрити головне вікно 3ds Max).
  - Прапорець `run_before_ignore: true` — закривається **до** завершення watcher на головному вікні.
- **Яка дія виконується:** `click_control` → `InvokePattern` натискає **`Close`** (`QMessageWidget.closePushButton`).


