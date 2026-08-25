@echo off
rem ============================================================
rem  Anonymizer Proxy — установщик Windows (обёртка install.ps1)
rem  Двойной клик: запускает PowerShell-установщик.
rem ============================================================
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
if errorlevel 1 (
    echo.
    echo [ОШИБКА] Установка завершилась с ошибкой.
    pause
    exit /b 1
)
pause
