@echo off
rem ============================================================
rem  Anonymizer Proxy proxy server launcher (Windows).
rem  Runs ONLY through the project virtual environment (.venv):
rem  dependencies (gliner, natasha, onnxruntime) are installed
rem  there; launching with the system python.exe fails with
rem  "NER model is not loaded".
rem
rem  Behavior:
rem  - if the proxy is ALREADY RUNNING, this script only opens
rem    the settings page (/env-editor) in the default browser
rem    and does NOT start a second instance;
rem  - otherwise the server starts in ITS OWN console window and
rem    the settings page opens in the browser once the server is
rem    ready. Closing the settings page does NOT stop the server.
rem
rem  IMPORTANT: the server window title is anonymizer-proxy.
rem  Ctrl+C sent to the terminal where this script runs (for
rem  example, the Cancel button in Cline sends Ctrl+C to the
rem  terminal) must NOT stop the server: console control events
rem  are delivered only to processes attached to the same
rem  console. Run with "--foreground" to keep the old behavior
rem  (server in this console; Ctrl+C stops it; no settings page).
rem
rem  NOTE: keep this file ASCII-only (see install.cmd).
rem ============================================================
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] .venv\Scripts\python.exe not found.
    echo Run the installer first: install.cmd
    exit /b 1
)

if /i "%~1"=="--foreground" goto foreground

rem Proxy already running? Then just open the settings page.
".venv\Scripts\python.exe" -m anonymizer_proxy.launcher check >nul 2>&1
if errorlevel 1 goto startserver
".venv\Scripts\python.exe" -m anonymizer_proxy.launcher open
echo Proxy is already running: settings page opened in your browser.
exit /b 0

:startserver
echo Starting anonymizer-proxy in a separate console window
echo (window title: anonymizer-proxy). The settings page will open
echo in your browser once the server is ready.
echo To stop the server: close that window or press Ctrl+C inside it.
start "anonymizer-proxy" /D "%~dp0" ".venv\Scripts\python.exe" -m anonymizer_proxy.main --open-settings %*
exit /b 0

:foreground
shift
".venv\Scripts\python.exe" -m anonymizer_proxy.main %*