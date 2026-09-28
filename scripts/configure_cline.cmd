@echo off
rem ============================================================
rem  Connect the standalone Cline desktop app to anonymizer-proxy.
rem  Thin wrapper: runs scripts\configure_cline.py through the
rem  project virtual environment (.venv), same as start_proxy.cmd.
rem
rem  Usage (from the project root or by double click):
rem    scripts\configure_cline.cmd [--set-active] [--dry-run] [...]
rem  See: .venv\Scripts\python.exe scripts\configure_cline.py --help
rem
rem  NOTE: keep this file ASCII-only (see install.cmd).
rem ============================================================
setlocal
set "ROOT=%~dp0.."
set "PYTHONIOENCODING=utf-8"
chcp 65001 >nul

if not exist "%ROOT%\.venv\Scripts\python.exe" (
    echo [ERROR] .venv\Scripts\python.exe not found.
    echo Run the installer first: install.cmd
    exit /b 1
)

"%ROOT%\.venv\Scripts\python.exe" -X utf8 "%~dp0configure_cline.py" %*
exit /b %ERRORLEVEL%
