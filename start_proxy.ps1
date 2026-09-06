# Запуск прокси-сервера анонимизации ТОЛЬКО через .venv.
# Пакет gliner установлен только в виртуальном окружении: запуск глобальным
# python.exe приводит к ошибке «NER-модель не загрузилась» при анонимизации.
#
# По умолчанию сервер стартует в ОТДЕЛЬНОМ окне консоли: Ctrl+C в текущем
# терминале (например, кнопка Cancel в Cline отправляет Ctrl+C в терминал)
# не должен останавливать сервер — события консоли доставляются только
# процессам этой же консоли. Вариант -Foreground — прежнее поведение
# (сервер в текущей консоли, Ctrl+C его останавливает).
#
# Поведение по умолчанию (ярлык): если прокси УЖЕ запущен — просто
# открывается страница настроек /env-editor в браузере (второй экземпляр
# не поднимается); иначе сервер стартует и страница настроек откроется,
# когда он будет готов. Закрытие страницы прокси не останавливает.
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

# Прокси уже запущен — открываем страницу настроек и выходим.
& $py -m anonymizer_proxy.launcher check *> $null
if ($LASTEXITCODE -eq 0) {
    & $py -m anonymizer_proxy.launcher open
    Write-Host "Прокси уже запущен — страница настроек открыта в браузере."
    exit 0
}

Write-Host "Запуск anonymizer-proxy в отдельном окне консоли..."
Write-Host "Остановка сервера: закрыть это окно или нажать Ctrl+C внутри него."
Write-Host "Страница настроек откроется в браузере, когда сервер будет готов."
$argList = @("-m", "anonymizer_proxy.main", "--open-settings") + @($args)
Start-Process -FilePath $py -ArgumentList $argList -WorkingDirectory $PSScriptRoot