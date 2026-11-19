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
