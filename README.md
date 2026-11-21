# 3ds Max (2024–2028) VM Automation (Python CLI + AutoHotkey v2)

Інструментарій для автоматизованого встановлення, тестування першого запуску, проклікування спливаючих вікон та деплою скриптів у **Autodesk 3ds Max 2024–2028** всередині віртуальної машини (**Hyper-V**).

## Структура диска `Z:\` на ВМ

```text
Z:\
├── 1_Restore_Worker.bat    # Відновити стан ВМ з _vm_state + запустити GitHub Actions Runner (в 1 вікні)
├── 2_Install_3dsMax.bat    # Автопошук ISO (2024-2028) у C:\..F:\Install та Z:\3dsMax\Install + мультиустановка
├── 3_Start_MaxAuto.bat     # Запустити max-auto (автоматично відновлює Python/venv з _vm_state при потребі)
├── 4_Backup_VM_State.bat   # Зберегти поточний стан ВМ (раннер, Python, venv, ключі) у _vm_state
├── 3dsMax\
│   ├── Install\            # ISO-образ(и)
│   ├── 2025.3\             # 3dsmax.exe + ярлик
│   └── 2027.2\             # 3dsmax.exe + ярлик
├── max-auto\               # Цей репозиторій
│   ├── ahk\actions.ahk     # Виконавець дій (кліки, клавіші, DPI-aware) на AutoHotkey v2
│   ├── cli\                # Python CLI (`max-auto`: inspect, watch, run, wait, deploy-scripts)
│   ├── config\rules.yaml   # Правила проклікування вікон для 3ds Max 2024–2028 та Autodesk Licensing
│   ├── logs\               # Дампи та скріншоти невідомих попапів
│   ├── max_scripts\        # Скрипти для деплою (.ms/.mse/.mcr/.mzp)
│   ├── vm\                 # PowerShell-скрипти (Install-3dsMax, оркестратор, бекап та відновлення ВМ)
│   └── run_max_auto.bat    # Інтерактивний лаунчер max-auto
├── tools\UAC\              # Увімкнення / вимкнення UAC
└── _vm_state\              # Офлайн-бекап стану ВМ для переживання відкатів чекпойнтів Hyper-V
```

---

## Швидкий старт після відкату чекпойнта Hyper-V

1. **Відновлення ВМ + запуск воркера:** `Z:\1_Restore_Worker.bat`
2. **Установка 3ds Max (2024–2028):** `Z:\2_Install_3dsMax.bat`
3. **Автопроклікування вікон (`max-auto`):** `Z:\3_Start_MaxAuto.bat`
4. **Оновити бекап у `Z:\_vm_state`** *(лише якщо міняли воркер / Python / токени)*: `Z:\4_Backup_VM_State.bat`

---

## Команди CLI (`python -m cli.main` або `max-auto`)

### 1. Інспекція вікон (`inspect`)
```powershell
python -m cli.main inspect
python -m cli.main inspect --all-windows
```
Виводить заголовки, процеси, тексти та інтерактивні елементи (`ButtonControl`, `CheckBoxControl` тощо) з їхніми `name`, `automation_id` та координатами `center=(x, y)`.

### 2. Спостереження та навчання (`watch`)
```powershell
python -m cli.main watch --dry-run
python -m cli.main watch --stop-on-ready
```
Клікає відомі вікна з `config/rules.yaml`, а невідомі зберігає в `logs/unknown_popups/` (JSON + PNG).

### 3. Очікування конкретного вікна (`wait`)
```powershell
python -m cli.main wait --rule "Privacy" --act --timeout 60
```

### 4. Повний запуск (`run`)
Автоматично знаходить встановлений `3dsmax.exe` (2027 або 2025), запускає його (якщо ще не запущений) і проклікує всі стартові вікна до готовності головного робочого вікна:
```powershell
python -m cli.main run
```

### 5. Деплой скриптів (`deploy-scripts`)
```powershell
python -m cli.main deploy-scripts --version 2027
```
Копіює `.ms`, `.mse`, `.mcr`, `.mzp` із `max_scripts/` у `%LOCALAPPDATA%\Autodesk\3dsMax\<версія> - 64bit\ENU\`.

---

## Remote 3ds Max Deployment System (`max-operator` / `max-client` / `max-relay`)

Чисто-Python (`stdlib-only` у `remote/`) система віддаленого розгортання **Autodesk 3ds Max (2024–2028)** на ПК клієнта з підтримкою Direct LAN/тунелю та NAT-traversal Relay:

- **`max-operator`** (`python -m remote.operator.main`): Веб-пульт керування (HTML5/CSS/JS + SSE `/api/v1/ui/events`), каталог ISO з SHA-256 кешуванням і `Range` (`206 Partial Content`), роздача оновлень `app/`, генерація `connect.json` та `maxpair://...`.
- **`max-client`** (`python -m remote.client.main`): Портативний клієнтський агент (запускається через `packaging/Start-MaxAgent.bat` → `remote/client/bootstrap.py` з одноразовим UAC та авто-відкатом) + локальний Status UI (`127.0.0.1:8770`, без `tkinter`, відкривається в Edge `--app=...` для TeamViewer).
- **`max-relay`** (`python -m remote.relay_server`): Вбудований чистий Python relay-сервер для роботи за NAT без прокидання портів.

### 7-етапна машина станів

`PREP` → `DOWNLOAD_ISO` *(локальна перевірка SHA-256 або докачування `.part`)* → `INSTALL_MAX` → `WAIT_LICENSE` *(ручне ліцензування через TeamViewer + кнопка підтвердження)* → `FIRST_RUN` *(автопроклікування через `max-auto --profile client`)* → `POST_CONFIG` *(1-клік: Corona, V-Ray, теми, quad-меню, скрипти, `verify_install.ms`)* → `DONE`.

### Швидкий старт

1. **Direct режим (LAN / тунель):**
```bash
python -m remote.operator.main --host 0.0.0.0 --port 8765 --iso-dir ./isos --updates-dir ./updates --no-browser
# Завантажте connect.json з http://127.0.0.1:8765/api/v1/ui/pairing/connect.json
python -m remote.client.main --connect-file connect.json --iso-dir ./client_isos --driver simulated --headless
```

2. **Relay режим (NAT traversal):**
```bash
python -m remote.relay_server --host 0.0.0.0 --port 8766
python -m remote.operator.main --mode relay --relay-url http://127.0.0.1:8766 --iso-dir ./isos --no-browser
python -m remote.client.main --connect-file connect.json --driver simulated --headless
```

### Портативна збірка, автооновлення та тести

```bash
# Збірка пакета оновлення app/ (manifest.json + zip)
python -m packaging.build_app_update --version 0.3.0 --out-dir ./updates

# Збірка портативного клієнта для Windows (папка: python-embed + AutoHotkey64.exe)
python -m packaging.build_portable_client --out-dir ./dist/MaxAgent

# Або один саморозпаковний .exe для TeamViewer (на Windows, розпаковується в .\MaxAgent\ поряд із .exe):
# .\packaging\build_sfx_exe.ps1 -OutExe dist\MaxAgent-Portable.exe -Version 1.0.0

# Запуск усіх unit- та інтеграційних тестів
python3 -m unittest discover -s tests -p "test_*.py"
```

Детальніше див. у [docs/REMOTE_DEPLOY.md](docs/REMOTE_DEPLOY.md).

