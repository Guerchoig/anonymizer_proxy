@echo off
chcp 65001 >nul
rem ============================================================
rem  Anonymizer Proxy — установщик Windows (обёртка install.ps1)
rem
rem  Запускайте ЭТОТ файл (двойной клик), а не install.ps1:
rem  1) он снимает пометку «скачано из интернета» (Mark of the Web),
rem     из-за которой политика RemoteSigned требует цифровую подпись;
rem  2) он вызывает PowerShell с -ExecutionPolicy Bypass — политика
rem     выполнения скриптов не мешает установке.
rem ============================================================
cd /d "%~dp0"

echo Снимаю блокировку скачанных файлов (Unblock-File)...
powershell -NoProfile -Command "Get-ChildItem -LiteralPath '.' | Unblock-File" >nul 2>&1

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
if errorlevel 1 (
    echo.
    echo [ОШИБКА] Установка завершилась с ошибкой. Возможные причины:
    echo.
    echo  1^) Политика выполнения задана групповой политикой ^(AllSigned^):
    echo     текст ошибки содержит «не может быть переопределена политикой,
    echo     заданной на этом компьютере». Обратитесь в IT или выполните
    echo     в PowerShell от администратора:
    echo       Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
    echo       .\install.ps1
    echo.
    echo  2^) Нет доступа в интернет для скачивания uv/Python/моделей.
    echo.
    pause
    exit /b 1
)
pause
