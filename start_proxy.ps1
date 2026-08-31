# Запуск прокси-сервера анонимизации ТОЛЬКО через .venv.
# Пакет gliner установлен только в виртуальном окружении: запуск глобальным
# python.exe приводит к ошибке «NER-модель не загрузилась» при анонимизации.
#
# По умолчанию сервер стартует в ОТДЕЛЬНОМ окне консоли: Ctrl+C в текущем
# терминале (например, кнопка Cancel в Cline отправляет Ctrl+C в терминал)
# не должен останавливать сервер — события консоли доставляются только
# процессам этой же консоли. Вариант -Foreground — прежнее поведение
# (сервер в текущей консоли, Ctrl+C его останавливает).
param([switch]$Foreground)

Set-Location -Path $PSScriptRoot

$py = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Host "[ОШИБКА] Не найден .venv\Scripts\python.exe" -ForegroundColor Red
    Write-Host "Создайте окружение: python -m venv .venv; .venv\Scripts\python.exe -m pip install -r requirements.txt"
    exit 1
}

if ($Foreground) {
    & $py -m anonymizer_proxy.main @args
    exit $LASTEXITCODE
}

Write-Host "Запуск anonymizer-proxy в отдельном окне консоли..."
Write-Host "Остановка сервера: закрыть это окно или нажать Ctrl+C внутри него."
$argList = @("-m", "anonymizer_proxy.main") + @args
Start-Process -FilePath $py -ArgumentList $argList -WorkingDirectory $PSScriptRoot