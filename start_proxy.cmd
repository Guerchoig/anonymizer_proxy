@echo off
rem ============================================================
rem  Anonymizer Proxy proxy server launcher (Windows).
rem  Runs ONLY through the project virtual environment (.venv):
rem  dependencies (gliner, natasha, onnxruntime) are installed
rem  there; launching with the system python.exe fails with
rem  "NER model is not loaded".
rem
rem  NOTE: keep this file ASCII-only (see install.cmd).
rem ============================================================
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] .venv\Scripts\python.exe not found.
    echo Run the installer first: install.cmd
    exit /b 1
)

".venv\Scripts\python.exe" -m anonymizer_proxy.main %*
