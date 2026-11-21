# Remote 3ds Max Deployment Guide

This guide covers building, pairing, running, and updating the pure-Python remote 3ds Max deployment system (`sergsuf/VM`) across an Operator PC and a Client PC (or Windows VM).

---

## 1. Architecture & Connection Modes

The platform uses a pure-Python standard-library architecture (`remote/`) with zero Node or Tauri build steps:

- **Direct Mode (`direct`)**: The Client Agent connects directly over HTTP/1.1 (LAN, VPN, or SSH/port-forward tunnel) to `remote.operator.server`. The Operator Web UI receives live updates via Server-Sent Events (`GET /api/v1/ui/events`) and serves ISO/update files with HTTP `206 Partial Content` range streaming.
- **Relay Mode (`relay`)**: When the Client PC is behind NAT or a restrictive firewall (typical during TeamViewer support sessions), both the Operator and the Client connect outbound to `remote.relay_server` (`max-relay`). Commands, heartbeats, state updates, logs, and chunked range downloads are routed per `session_id` without requiring inbound firewall rules on either PC.

---

## 2. Building the Portable Client Bundle

On Windows (full embedded runtime + AutoHotkey v2 + pip dependencies):

```powershell
# Option A: Portable folder (dist\MaxAgent-Portable)
.\packaging\build_portable_client.ps1 -OutDir dist\MaxAgent-Portable -Version 1.0.0

# Option B: Single self-extracting .exe for TeamViewer delivery (unpacks into .\MaxAgent\ next to the .exe)
.\packaging\build_sfx_exe.ps1 -OutExe dist\MaxAgent-Portable.exe -Version 1.0.0
```

Cross-platform / offline layout verification (Linux or Windows):

```bash
python3 packaging/build_portable_client.py --version 1.0.0 --out-dir dist/MaxAgent-Portable --offline
```

Generated portable layout:

- `Start-MaxAgent.bat`: Thin `%~dp0` launcher (no `.vbs`, `wscript`, or `cscript`) that prepends `tools\ahk` to `PATH` and invokes `runtime\python.exe -u runtime\bootstrap.py --root "%ROOT%" %*`.
- `runtime\python312._pth`: Configured with `python312.zip`, `.`, `Lib\site-packages`, and an uncommented `import site` line so bundled packages (`PyYAML`, `click`, `Pillow`, `psutil`, `uiautomation`) load cleanly in `python-embed`.
- `runtime\bootstrap.py`: Self-contained elevated supervisor (`remote/client/bootstrap.py`).
- `tools\ahk\AutoHotkey64.exe`: Bundled AutoHotkey v2 runtime resolved automatically by `cli/ahk_bridge.py`.
- `app\active.json` and `app\releases\<version>\`: Versioned application code (`remote/`, `cli/`, `ahk/`, `config/`, `max_scripts/`, `vm/`).

---

## 3. Pairing & Security Controls (TeamViewer Handoff)

1. Start the Operator Control Panel (`max-operator --iso-dir <isos> --assets-dir <assets> --updates-dir <updates>`).
2. Export `connect.json` (or copy the `maxpair://...` URI) from the Operator UI and place `connect.json` beside `Start-MaxAgent.bat` on the Client PC.
3. Launch `Start-MaxAgent.bat` on the Client PC inside an interactive desktop session (`SessionId > 0`; Session 0 services are rejected).
4. **Single UAC Elevation**: `bootstrap.py` displays a pre-UAC notice (`MessageBoxW`) and elevates once via `ShellExecuteW("runas")`. All subsequent child restarts (including auto-updates) run inside the already-elevated supervisor without triggering a second UAC prompt.
5. **Smart App Control & Windows Defender**:
   - **Smart App Control**: The agent reads `VerifiedAndReputablePolicyState` (`0=Off`, `1=Enforce`, `2=Evaluation`) and `CiTool.exe -lp -json`. From the Operator UI or Client Status UI, you can trigger the guided Smart App Control disable flow (`windowsdefender://smartapp/`).
   - **Defender**: The agent reads `Get-MpComputerStatus` (including `IsTamperProtected`), adds expanded-path `Add-MpPreference -ExclusionPath` exclusions for the portable bundle and ISO cache, and attempts real-time protection disable with honest read-back verification and `windowsdefender://threatsettings/` fallback if Tamper Protection is enabled.

---

## 4. 7-Stage Deployment Workflow

1. `PREP`: System checks, UAC/Smart App Control/Defender readiness, and ISO pre-check across `C:\..F:\Install`, `Z:\3dsMax\Install`, and cache.
2. `DOWNLOAD_ISO`: Resumable HTTP Range transfer (`.part` + `.part.meta.json`) with SHA-256 verification, skipped automatically if a matching ISO already exists locally.
3. `INSTALL_MAX`: Runs `vm/Install-3dsMax.ps1 -QuietConsole` and tails `3dsMax-<ver>-status.txt` (`<Percent>%|<Message>`).
4. `WAIT_LICENSE`: Pauses cleanly for manual Autodesk licensing/activation on the Client PC (e.g., via TeamViewer). Click **Confirm License** in the Operator UI when ready.
5. `FIRST_RUN`: Launches 3ds Max (`de_elevate=True`) and runs `max-auto` (`cli/main.py --profile client`) to dismiss startup popups. If licensing is still missing, it captures a screenshot and returns to `WAIT_LICENSE`.
6. `POST_CONFIG`: 1-click declarative pipeline (`config/post_config.yaml`) that closes 3ds Max, backs up `ENU/`, installs Corona/V-Ray, applies version-aware themes/quad-menus/hotkeys, deploys scripts/macros/`.mzp`, and runs `max_scripts/verify_install.ms`.
7. `DONE`: Reports full verification summary (`max_verify.json`) to the Operator UI.

---

## 5. Background `app/` Auto-Updates & Rollback

Build an update bundle and `manifest.json`:

```bash
python3 packaging/build_app_update.py --version 1.1.0 --out-dir dist/updates --notes "Bug fixes"
```

When `PUSH_UPDATE` is sent (or polled from `/api/v1/updates/latest`):

- `AppUpdater` (`remote/updater.py`) checks SemVer (`manifest.version > current_version`), `blocked_versions`, `min_runtime_version`, and `DeploymentStateMachine.can_accept_update()` (deferring updates during active `RUNNING` stages such as `INSTALL_MAX`).
- Downloads the zip, verifies `size_bytes` and `sha256`, performs Zip-Slip-safe extraction into `app/releases/<version>/`, and atomically swaps `app/active.json` with `pending_verify: true`.
- The child exits with `UPDATE_RESTART_EXIT_CODE = 42`. `bootstrap.py` re-reads `app/active.json` and launches the new release immediately without a second UAC prompt.
- On a healthy boot, `mark_healthy_boot(app_dir)` clears `pending_verify` and resets `attempts = 0`.
- If the new release crashes before clearing `pending_verify`, `bootstrap.py` retries up to `MAX_VERIFY_ATTEMPTS = 2` times and then automatically rolls back `app/active.json` to `previous` and adds the broken version to `blocked_versions`.
