# 3ds Max 2025 VM Automation (Python CLI + AutoHotkey v2)

Інструментарій для автоматизованого тестування першого запуску, проклікування спливаючих вікон та встановлення скриптів у **Autodesk 3ds Max 2025** всередині віртуальної машини (**Hyper-V**).

## Структура проєкту

```text
├── ahk/
│   └── actions.ahk            # Виконавець дій (кліки, ввод клавіш/тексту) на AutoHotkey v2
├── cli/
│   ├── main.py                # Головний CLI (`max-auto`)
│   ├── ui_inspector.py        # Сканер вікон та кнопок через Windows UI Automation (UIA)
│   ├── watcher.py             # Цикл відстеження вікон + режим навчання (Learn Mode)
│   ├── ahk_bridge.py          # Міст Python -> AutoHotkey64.exe
│   └── max_deployer.py        # Копіювання .ms/.mse/.mcr/.mzp і запуск 3ds Max 2025
├── config/
│   └── rules.yaml             # Правила: які вікна і які кнопки натискати
├── max_scripts/
│   └── verify_install.ms      # Тестовий startup-скрипт (створює маркер у %TEMP%)
└── vm/
    └── hyperv_checkpoint.ps1  # Скрипт для хоста Hyper-V: відкат чекпойнту + запуск ВМ
```

---

## Підготовка ВМ (один раз перед створенням Clean Checkpoint)

1. **Установіть у Windows ВМ**:
   - **Python 3.10+** (з додаванням у `PATH`).
   - **AutoHotkey v2** (стандартний шлях `C:\Program Files\AutoHotkey\v2\AutoHotkey64.exe`).
2. **Склонуйте/скопіюйте репозиторій у ВМ** і встановіть залежності:
   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -e .
   ```
3. **Зробіть Checkpoint у Hyper-V** (наприклад, `Clean-Installed-2025`), щоб перед кожним тестом миттєво повертатися до чистого стану перед першим запуском 3ds Max 2025:
   ```powershell
   .\vm\hyperv_checkpoint.ps1 -VmName "3dsMax2025-Test" -CheckpointName "Clean-Installed-2025" -WaitHeartbeat
   ```

---

## Як використовувати

### 1. Режим інспекції (подивитися, які кнопки є на екрані зараз)
Коли вискочило вікно 3ds Max або Autodesk, виконайте:
```powershell
max-auto inspect
```
Або для всіх вікон у системі:
```powershell
max-auto inspect --all-windows
```
CLI виведе заголовок вікна, процес, усі кнопки (`ButtonControl`, `CheckBoxControl` тощо), їхні `name`, `automation_id` та точні координати `center=(x, y)`.

### 2. Режим спостереження та навчання (`watch`)
Запустіть watcher перед першим стартом 3ds Max (або у фоні):
```powershell
# --dry-run лише покаже, що б він натиснув, без реальних кліків
max-auto watch --dry-run

# Бойовий режим: клікає відомі вікна з config/rules.yaml,
# а невідомі вікна зберігає в logs/unknown_popups/ (JSON + скріншот PNG)
max-auto watch
```
Коли з'явиться нове невідоме вікно:
- CLI автоматично збереже JSON з усіма кнопками та PNG-скріншот у `logs/unknown_popups/`.
- Ви додаєте нове правило в `config/rules.yaml` і відкочуєте чекпойнт Hyper-V для повторного тесту.

### 3. Установка скриптів у 3ds Max 2025
Покладіть потрібні `.ms`, `.mse`, `.mcr`, `.mzp` у папку `max_scripts/` і запустіть:
```powershell
max-auto deploy-scripts
```
Скрипти автоматично скопіюються в `%LOCALAPPDATA%\Autodesk\3dsMax\2025 - 64bit\ENU\scripts\startup\` та `usermacros\`.

### 4. Повний прогін в один клік (`run`)
Деплоїть скрипти → запускає `3dsmax.exe` → відстежує та проклікує всі стартові вікна до появи головного робочого вікна 3ds Max 2025:
```powershell
max-auto run
```
