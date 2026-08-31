@echo off
rem ============================================================
rem  Anonymizer Proxy proxy server launcher (Windows).
rem  Runs ONLY through the project virtual environment (.venv):
rem  dependencies (gliner, natasha, onnxruntime) are installed
rem  there; launching with the system python.exe fails with
rem  "NER model is not loaded".
rem
rem  IMPORTANT: the server starts in ITS OWN console window.
rem  Ctrl+C sent to the terminal where this script runs (for
rem  example, the Cancel button in Cline sends Ctrl+C to the
rem  terminal) must NOT stop the server: console control events
rem  are delivered only to processes attached to the same
rem  console. Run with "--foreground" to keep the old behavior
rem  (server in this console; Ctrl+C stops it).
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

echo Starting anonymizer-proxy in a separate console window
echo (window title: anonymizer-proxy).
echo To stop the server: close that window or press Ctrl+C inside it.
start "anonymizer-proxy" /D "%~dp0" ".venv\Scripts\python.exe" -m anonymizer_proxy.main %*
exit /b 0

:foreground
shift
".venv\Scripts\python.exe" -m anonymizer_proxy.main %*