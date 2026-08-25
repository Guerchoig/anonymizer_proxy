@echo off
rem ============================================================
rem  Запуск прокси-сервера анонимизации ТОЛЬКО через .venv.
rem  Пакет gliner установлен только в виртуальном окружении:
rem  запуск глобальным python.exe приводит к ошибке
rem  «NER-модель не загрузилась» при анонимизации файлов.
rem ============================================================
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [ОШИБКА] Не найдено .venv\Scripts\python.exe
    echo Создайте окружение: python -m venv .venv ^&^& .venv\Scripts\python.exe -m pip install -r requirements.txt
    exit /b 1
)

".venv\Scripts\python.exe" -m anonymizer_proxy.main %*
