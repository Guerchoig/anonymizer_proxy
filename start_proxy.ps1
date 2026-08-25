# Запуск прокси-сервера анонимизации ТОЛЬКО через .venv.
# Пакет gliner установлен только в виртуальном окружении: запуск глобальным
# python.exe приводит к ошибке «NER-модель не загрузилась» при анонимизации.
Set-Location -Path $PSScriptRoot

$py = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Host "[ОШИБКА] Не найден .venv\Scripts\python.exe" -ForegroundColor Red
    Write-Host "Создайте окружение: python -m venv .venv; .venv\Scripts\python.exe -m pip install -r requirements.txt"
    exit 1
}

& $py -m anonymizer_proxy.main @args
