# ============================================================
#  Anonymizer Proxy — установщик Windows
#  Один запуск: окружение → зависимости → модели → self-test → ярлык.
#  Повторный запуск идемпотентен (ничего не перекачивает без нужды).
# ============================================================
[CmdletBinding()]
param(
    [switch]$SkipModels,   # пропустить прогрев NER-моделей
    [switch]$SkipSelfTest  # пропустить самопроверку
)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

function Write-Step([string]$msg) {
    Write-Host ""
    Write-Host "==> $msg" -ForegroundColor Cyan
}

# ---------- 1. uv ----------
Write-Step "Проверка uv (менеджер окружения и Python)"
$uv = Get-Command uv -ErrorAction SilentlyContinue
if (-not $uv) {
    Write-Host "uv не найден — устанавливаю standalone-версию…"
    irm https://astral.sh/uv/install.ps1 | iex
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
    $uv = Get-Command uv -ErrorAction SilentlyContinue
    if (-not $uv) {
        Write-Host "[ОШИБКА] uv не установился. Установите вручную: https://docs.astral.sh/uv/" -ForegroundColor Red
        exit 1
    }
}
Write-Host "uv: $((& uv --version).Trim())"

# ---------- 2. Детект железа ----------
Write-Step "Определение аппаратного ускорителя"
$flavor = "cpu"
if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
    & nvidia-smi | Out-Null
    if ($LASTEXITCODE -eq 0) { $flavor = "cuda" }
}
if ($flavor -eq "cpu") { $flavor = "directml" }  # Windows без NVIDIA → DirectML
switch ($flavor) {
    "cuda"     { Write-Host "Обнаружен NVIDIA GPU → extra 'cuda' (onnxruntime-gpu 1.24.4 + CUDA DLL из pip)" }
    "directml" { Write-Host "NVIDIA не найден → extra 'directml' (AMD/Intel GPU, NPU через DirectML)" }
    default    { Write-Host "GPU не обнаружен → extra 'cpu'" }
}

# ---------- 3. Зависимости ----------
Write-Step "Установка зависимостей (uv sync --locked --extra $flavor)"
& uv sync --locked --extra $flavor
if ($LASTEXITCODE -ne 0) {
    Write-Host "[ОШИБКА] uv sync завершился с кодом $LASTEXITCODE" -ForegroundColor Red
    exit 1
}
$py = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"

# ---------- 4. Конфигурация .env ----------
Write-Step "Конфигурация .env"
if (Test-Path ".env") {
    Write-Host ".env уже существует — не трогаю (ваши настройки сохранены)."
} else {
    Copy-Item ".env.example" ".env"
    # PROXY_API_TOKEN — криптостойкий случайный токен
    $bytes = New-Object byte[] 32
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $token = [Convert]::ToBase64String($bytes).TrimEnd("=").Replace("+", "-").Replace("/", "_")
    (Get-Content ".env" -Raw) -replace "PROXY_API_TOKEN=REPLACE_WITH_RANDOM_TOKEN", "PROXY_API_TOKEN=$token" | Set-Content ".env" -NoNewline -Encoding UTF8
    Write-Host "Создан .env, сгенерирован PROXY_API_TOKEN."
    $key = Read-Host "Введите OPENROUTER_API_KEY (sk-or-v1-…) или Enter, чтобы задать позже"
    if ($key) {
        (Get-Content ".env" -Raw) -replace "OPENROUTER_API_KEY=sk-or-v1-REPLACE_WITH_YOUR_KEY", "OPENROUTER_API_KEY=$key" | Set-Content ".env" -NoNewline -Encoding UTF8
        Write-Host "Ключ OpenRouter записан."
    } else {
        Write-Host "Ключ не задан — прокси запустится, но облачные запросы не пойдут (можно задать позже в .env)."
    }
}

# ---------- 5. Прогрев моделей ----------
if (-not $SkipModels) {
    Write-Step "Прогрев NER-моделей (GLiNER + Natasha, первый раз — скачивание)"
    & $py scripts\download_models.py
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[ПРЕДУПРЕЖДЕНИЕ] Прогрев моделей завершился с ошибками — см. выше." -ForegroundColor Yellow
    }
}

# ---------- 6. Самопроверка ----------
if (-not $SkipSelfTest) {
    Write-Step "Самопроверка установки"
    & $py scripts\install_selftest.py
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[ПРЕДУПРЕЖДЕНИЕ] Self-test FAIL — см. выше." -ForegroundColor Yellow
    }
}

# ---------- 7. Ярлык в меню Пуск ----------
Write-Step "Ярлык «Anonymizer Proxy» в меню Пуск"
try {
    $startMenu = [Environment]::GetFolderPath("StartMenu")
    $shortcutPath = Join-Path $startMenu "Programs\Anonymizer Proxy.lnk"
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($shortcutPath)
    $shortcut.TargetPath = Join-Path $PSScriptRoot "start_proxy.cmd"
    $shortcut.WorkingDirectory = $PSScriptRoot
    $shortcut.WindowStyle = 7  # свёрнуто
    $shortcut.Description = "Локальный прокси анонимизации для Cline"
    $shortcut.Save()
    Write-Host "Ярлык создан: $shortcutPath"
} catch {
    Write-Host "[ПРЕДУПРЕЖДЕНИЕ] Ярлык не создан: $_" -ForegroundColor Yellow
}

# ---------- Финал ----------
Write-Host ""
Write-Host "============================================================" -ForegroundColor Green
Write-Host " Установка завершена!" -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Green
Write-Host "Запуск:        start_proxy.cmd (или ярлык в меню Пуск)"
Write-Host "Проверка:      http://127.0.0.1:8081/health"
Write-Host ""
Write-Host "Подключение Cline (расширение VS Code):"
Write-Host "  Base URL:  http://127.0.0.1:8081/v1"
Write-Host "  API Key:   любое значение (прокси его игнорирует)"
Write-Host "  Model:     любое значение"
Write-Host ""
Write-Host "Не забудьте скопировать правила .clinerules в Cline —"
Write-Host "они нужны для корректной работы с анонимизированными файлами."
