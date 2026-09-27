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

# ---------- 0. Системные требования ----------
Write-Step "Проверка системных требований"

# Архитектура и ОС: только 64-разрядная Windows 10+
if (-not [Environment]::Is64BitOperatingSystem) {
    Write-Host "[ОШИБКА] Требуется 64-разрядная Windows." -ForegroundColor Red
    exit 1
}
$ver = [Environment]::OSVersion.Version
if ($ver.Major -lt 10) {
    Write-Host "[ОШИБКА] Требуется Windows 10 или новее (обнаружена $($ver.Major).$($ver.Minor))." -ForegroundColor Red
    exit 1
}
Write-Host ("ОС: Windows {0}.{1}, x64" -f $ver.Major, $ver.Minor)

# Оперативная память: минимум 8 ГБ (рекомендуется 16)
$ramGb = [math]::Round((Get-CimInstance -ClassName Win32_ComputerSystem).TotalPhysicalMemory / 1GB, 1)
if ($ramGb -lt 8) {
    Write-Host "[ОШИБКА] Недостаточно оперативной памяти: ${ramGb} ГБ (минимум 8 ГБ)." -ForegroundColor Red
    exit 1
}
elseif ($ramGb -lt 16) {
    Write-Host "[ПРЕДУПРЕЖДЕНИЕ] ОЗУ ${ramGb} ГБ — работать будет, но рекомендуется 16 ГБ." -ForegroundColor Yellow
}
else {
    Write-Host "ОЗУ: ${ramGb} ГБ"
}

# Свободное место на диске проекта: минимум 10 ГБ (рекомендуется 20):
# зависимости ~7 ГБ (torch и пр.) + CUDA-DLL до ~1,5 ГБ + модели ~1,5 ГБ
try {
    $drive = (Get-Item -LiteralPath $PSScriptRoot).PSDrive
    $freeGb = [math]::Round($drive.Free / 1GB, 1)
    if ($freeGb -lt 10) {
        Write-Host "[ОШИБКА] Мало свободного места на диске $($drive.Name): ${freeGb} ГБ (минимум 10 ГБ)." -ForegroundColor Red
        exit 1
    }
    elseif ($freeGb -lt 20) {
        Write-Host "[ПРЕДУПРЕЖДЕНИЕ] Свободно ${freeGb} ГБ на диске $($drive.Name) — рекомендуется 20 ГБ." -ForegroundColor Yellow
    }
    else {
        Write-Host "Свободное место: ${freeGb} ГБ"
    }
}
catch {
    Write-Host "[ПРЕДУПРЕЖДЕНИЕ] Не удалось определить свободное место на диске: $_" -ForegroundColor Yellow
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

# ---------- 3. Общий llama-рантайм (llama.cpp + GGUF-модели) ----------
# Бинарь llama-server и модели — в ЕДИНОМ каталоге машины
# (%LLAMA_RUNTIME_DIR% / %LOCALAPPDATA%\llama-runtime), общем с другими
# проектами (hermes-disk-search и т.п.): один вариант сборки (cuda|vulkan),
# один набор моделей, синхронная смена чат-модели во всех проектах.
Write-Step "Общий llama-рантайм машины (llama-server + GGUF-модели)"
try {
    & powershell -NoProfile -ExecutionPolicy Bypass `
        -File (Join-Path $PSScriptRoot "scripts\ensure_llama_runtime.ps1") `
        -Models chat `
        -ProjectName "anonymizer_proxy" `
        -ProjectRoot $PSScriptRoot `
        -RestartArgs "-m anonymizer_proxy.llm_server restart"
    if ($LASTEXITCODE -ne 0) { throw "ensure_llama_runtime.ps1 завершился с кодом $LASTEXITCODE" }
} catch {
    Write-Host "[ПРЕДУПРЕЖДЕНИЕ] Общий llama-рантайм не готов: $_" -ForegroundColor Yellow
    Write-Host "Локальная модель не поднимется (облачные бэкенды работают как обычно)." -ForegroundColor Yellow
    Write-Host "Повторите позже: powershell -File scripts\ensure_llama_runtime.ps1 -Models chat" -ForegroundColor Yellow
}

# ---------- 4. Зависимости ----------
Write-Step "Установка зависимостей (uv sync --locked --extra $flavor)"
& uv sync --locked --extra $flavor
if ($LASTEXITCODE -ne 0) {
    Write-Host "[ОШИБКА] uv sync завершился с кодом $LASTEXITCODE" -ForegroundColor Red
    exit 1
}
$py = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"

# ---------- 4. Конфигурация .env ----------
Write-Step "Конфигурация .env"
# Все правки .env — через [IO.File] с явной кодировкой UTF-8 БЕЗ BOM.
# Get-Content/Set-Content в Windows PowerShell 5.1 читают файлы без BOM
# в системной ANSI-кодировке (CP1251) и пишут UTF-8 с BOM — из-за этого
# русский текст в .env превращался в кракозябры.
$encNoBom = New-Object System.Text.UTF8Encoding($false)
$envPath = Join-Path $PSScriptRoot ".env"
if (Test-Path ".env") {
    Write-Host ".env уже существует — не трогаю (ваши настройки сохранены)."
} else {
    Copy-Item ".env.example" ".env"
    $content = [IO.File]::ReadAllText($envPath, $encNoBom)

    # PROXY_API_TOKEN — криптостойкий случайный токен
    $bytes = New-Object byte[] 32
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $token = [Convert]::ToBase64String($bytes).TrimEnd("=").Replace("+", "-").Replace("/", "_")
    $content = $content.Replace("PROXY_API_TOKEN=REPLACE_WITH_RANDOM_TOKEN", "PROXY_API_TOKEN=$token")
    [IO.File]::WriteAllText($envPath, $content, $encNoBom)
    Write-Host "Создан .env, сгенерирован PROXY_API_TOKEN."

    while ($true) {
        $key = (Read-Host "Введите OPENROUTER_API_KEY (sk-or-v1-…), Enter — пропустить").Trim()
        if (-not $key) {
            Write-Host "[ВНИМАНИЕ] Ключ не задан: анонимизация работать будет, а вот запросы к облаку упадут с 401." -ForegroundColor Yellow
            Write-Host "Впишите ключ с https://openrouter.ai/keys в .env и перезапустите прокси." -ForegroundColor Yellow
            break
        }
        if ($key -notmatch '^sk-or-[A-Za-z0-9\-]{20,}$') {
            Write-Host "Ключ должен начинаться с sk-or- и состоять из латиницы/цифр/дефисов. Проверьте вставку и попробуйте ещё раз." -ForegroundColor Yellow
            continue
        }
        $content = [IO.File]::ReadAllText($envPath, $encNoBom)
        $content = $content.Replace("OPENROUTER_API_KEY=sk-or-v1-REPLACE_WITH_YOUR_KEY", "OPENROUTER_API_KEY=$key")
        [IO.File]::WriteAllText($envPath, $content, $encNoBom)
        Write-Host "Ключ OpenRouter записан."
        break
    }
    Write-Host ""
    Write-Host "Сразу после установки действует OpenRouter (нужен VPN)."
    Write-Host "Действующего провайдера можно сменить в любой момент: российские"
    Write-Host "GPTunneL, BotHub, AITUNNEL, GenAPI и свой endpoint работают без VPN —"
    Write-Host "форма http://127.0.0.1:8081/env-editor (выбор провайдера и модели),"
    Write-Host "POST /api/backend или переменная CLOUD_PROVIDER в .env."
}

# ---------- 4b. Миграция .env: недостающие ключи из .env.example ----------
# Установщик НЕ перезаписывает существующий .env (настройки пользователя
# сохраняются), но при обновлении поверх СТАРОЙ установки схема конфига
# меняется. Идемпотентно добавляем отсутствующие ключи (например, секцию
# LLM_SERVER_* после перехода с LM Studio на llama-server) — без них прокси
# молча работает в устаревшей конфигурации: LLM_SERVER_MODEL пуст, сервер не
# запускается, а «залипший» LOCAL_LLM_BASE_URL даёт 502 «llama-server не
# отвечает». Заодно уводятся в комментарий устаревшие ключи.
Write-Step "Миграция .env (недостающие ключи из .env.example)"
& $py scripts\ensure_env_keys.py
if ($LASTEXITCODE -ne 0) {
    Write-Host "[ПРЕДУПРЕЖДЕНИЕ] .env не дополнен — проверьте настройки" -ForegroundColor Yellow
    Write-Host "  локальной модели (LLM_SERVER_*) вручную: scripts\ensure_env_keys.py" -ForegroundColor Yellow
}

# ---------- 5b. GPU-аргументы llama-server под железо ----------
# llama.cpp по умолчанию считает всё на CPU; скрипт детектирует NVIDIA/VRAM
# и размер GGUF-модели, подбирает -ngl (99/16/0). Если пользователь сам
# задал -ngl в .env — не перезаписываем (детали: scripts/configure_llm_args.py).
Write-Step "Подбор GPU-аргументов llama-server (-ngl) под железо"
& $py scripts\configure_llm_args.py
if ($LASTEXITCODE -ne 0) {
    Write-Host "[ПРЕДУПРЕЖДЕНИЕ] Не удалось подобрать -ngl — .env оставлен без изменений." -ForegroundColor Yellow
}

# ---------- 6. Прогрев моделей ----------
if (-not $SkipModels) {
    Write-Step "Прогрев NER-моделей (GLiNER + Natasha, первый раз — скачивание)"
    & $py scripts\download_models.py
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[ПРЕДУПРЕЖДЕНИЕ] Прогрев моделей завершился с ошибками — см. выше." -ForegroundColor Yellow
    }
    # GGUF-модель локальной LLM уже обеспечена на шаге 3 (общий
    # llama-рантайм, models\chat общего каталога) — защита от повторной
    # загрузки при повторном запуске установщика.
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
    $iconPath = Join-Path $PSScriptRoot "icon.ico"
    if (Test-Path $iconPath) {
        $shortcut.IconLocation = "$iconPath, 0"
    }
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
Write-Host "Правила анонимизации берутся из папки проекта:"
Write-Host "  .clinerules уже в корне прокси (для Cline в этой папке)."
Write-Host "  Для другого проекта с документами выполните:"
Write-Host "    .venv\Scripts\python.exe scripts\install_rules.py <путь-к-проекту> --clinerules"
Write-Host "  (создаст AGENTS.md для Hermes и .clinerules для Cline)."
