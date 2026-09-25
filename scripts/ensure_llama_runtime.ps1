# ============================================================================
# Общий llama-рантайм машины: llama-server (одна сборка cuda|vulkan) и GGUF
# модели в ЕДИНОМ каталоге для всех проектов (anonymizer_proxy,
# hermes-disk-search и др.).
#
# SYNC-COPY: файл идентичен в обоих репозиториях
# (anonymizer_proxy/scripts/ensure_llama_runtime.ps1 и
#  hermes-disk-search/installers/ensure_llama_runtime.ps1).
# При правке синхронизировать копии вручную.
#
# Каталог: %LLAMA_RUNTIME_DIR% -> %LOCALAPPDATA%\llama-runtime.
# Раскладка: bin\llama-server.exe (+DLL), models\<role>\*.gguf,
#            models\chat\current.json, projects.json, version.json.
#
# Идемпотентен: повторный запуск (в т.ч. из установщика второго проекта)
# ничего не качает, если бинарь нужного варианта и модели уже на месте.
# Если модель уже скачана в старой установке LM Studio (~/.lmstudio/models),
# она копируется оттуда — без повторной загрузки (наследие ensure_models.ps1).
#
# Примеры:
#   ensure_llama_runtime.ps1 -Models chat
#   ensure_llama_runtime.ps1 -Models chat,embedding,rerank `
#       -ProjectName hermes-disk-search -ProjectRoot C:\...\hermes-disk-search `
#       -RestartArgs "-m hds.llama_server restart chat"
#   ensure_llama_runtime.ps1 -Force          # переустановить бинарь
# ============================================================================
param(
    [string]$RuntimeDir = "",
    [string]$Models = "chat",
    [switch]$Force,
    [string]$ProjectName = "",
    [string]$ProjectRoot = "",
    [string]$RestartArgs = ""
)

$ErrorActionPreference = "Continue"

$rt = $RuntimeDir
if (-not $rt) {
    if ($env:LLAMA_RUNTIME_DIR) { $rt = $env:LLAMA_RUNTIME_DIR }
    else { $rt = Join-Path $env:LOCALAPPDATA "llama-runtime" }
}
$binDir = Join-Path $rt "bin"
$llamaExe = Join-Path $binDir "llama-server.exe"
$versionFile = Join-Path $rt "version.json"
Write-Host "[..] Общий llama-рантайм: $rt"

# ---------- 1. Вариант сборки (одна на машину) ----------
$variant = "vulkan"
if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
    & nvidia-smi | Out-Null
    if ($LASTEXITCODE -eq 0) { $variant = "cuda" }
}
$haveVariant = ""
if (Test-Path $versionFile) {
    try { $haveVariant = (Get-Content $versionFile -Raw -Encoding UTF8 | ConvertFrom-Json).variant } catch { $haveVariant = "" }
}

# ---------- 2. Бинарь llama-server ----------
New-Item -ItemType Directory -Force -Path $binDir | Out-Null
$needBin = $Force -or (-not (Test-Path $llamaExe)) -or ($haveVariant -ne $variant)
if (-not $needBin) {
    Write-Host "[ok] llama-server уже установлен ($variant): $llamaExe"
} else {
    $assetPattern = "llama-*bin-win-$variant-x64*.zip"
    Write-Host "[..] Скачиваю пре-билд llama.cpp ($assetPattern)..."
    try {
        # latest-релиз может не содержать win-ассетов (nightly) — берём
        # первый из последних 10, где нужный ассет есть
        $rel = Invoke-RestMethod -Uri "https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=10" -TimeoutSec 30 |
            Where-Object { ($_.assets | Where-Object { $_.name -like $assetPattern }).Count -gt 0 } |
            Select-Object -First 1
        if (-not $rel) { throw "в последних релизах llama.cpp нет ассета $assetPattern" }
        $asset = $rel.assets | Where-Object { $_.name -like $assetPattern } | Select-Object -First 1
        $zipPath = Join-Path $env:TEMP $asset.name
        $ProgressPreference = "SilentlyContinue"
        Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $zipPath -TimeoutSec 900
        if ((Get-Item $zipPath).Length -ne $asset.size) {
            throw ("zip недокачан: получено {0}, ожидалось {1}" -f (Get-Item $zipPath).Length, $asset.size)
        }
        Expand-Archive -Path $zipPath -DestinationPath $binDir -Force
        Remove-Item $zipPath -ErrorAction SilentlyContinue
        # CUDA-сборке нужен runtime (cublas64_12.dll и др.)
        if ($variant -eq "cuda") {
            $cudart = $rel.assets | Where-Object { $_.name -like "cudart-llama-bin-win-cuda*x64.zip" } | Select-Object -First 1
            if ($cudart) {
                $cz = Join-Path $env:TEMP $cudart.name
                Invoke-WebRequest -Uri $cudart.browser_download_url -OutFile $cz -TimeoutSec 900
                Expand-Archive -Path $cz -DestinationPath $binDir -Force
                Remove-Item $cz -ErrorAction SilentlyContinue
            } else {
                Write-Host "[!!] В релизе нет cudart-llama-bin-win-cuda-*.zip — CUDA-сборка может не стартовать" -ForegroundColor Yellow
            }
        }
        # llama.cpp кладёт бинари в подпапку llama-<tag>-bin-win-.../ — поднимаем
        $nested = Get-ChildItem -Path $binDir -Recurse -Filter "llama-server.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($nested -and (Split-Path $nested.FullName) -ne $binDir) {
            Move-Item (Join-Path (Split-Path $nested.FullName) "*") $binDir -Force -ErrorAction SilentlyContinue
            Get-ChildItem -Path $binDir -Directory | Where-Object { $_.Name -like "llama-*-bin-win-*" } | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
        }
        & $llamaExe --version | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "llama-server --version упал с кодом $LASTEXITCODE" }
        $json = [pscustomobject]@{ variant = $variant; tag = $rel.tag_name; installed_at = (Get-Date -Format s) }
        [IO.File]::WriteAllText($versionFile, ($json | ConvertTo-Json), (New-Object System.Text.UTF8Encoding($false)))
        Write-Host "[ok] llama-server установлен ($variant, $($rel.tag_name)): $llamaExe" -ForegroundColor Green
    } catch {
        Write-Host "[!!] Не удалось скачать llama.cpp: $_" -ForegroundColor Yellow
        Write-Host "     Скачайте бинарь вручную с https://github.com/ggml-org/llama.cpp/releases" -ForegroundColor Yellow
        Write-Host "     и распакуйте в $binDir" -ForegroundColor Yellow
    }
}

# ---------- 3. GGUF-модели (общий каталог models\<role>\) ----------
# Пресеты: имя файла -> источник. Смена дефолтной чат-модели проекта —
# правка одной строки здесь + DEFAULT_CHAT в llama_runtime.py.
$presets = @{
    "chat" = @{
        file = "Qwen3.5-9B-Q6_K.gguf"
        url = "https://huggingface.co/unsloth/Qwen3.5-9B-GGUF/resolve/main/Qwen3.5-9B-Q6_K.gguf"
        minMB = 4000
    }
    "embedding" = @{
        file = "bge-m3-Q8_0.gguf"
        url = "https://huggingface.co/lm-kit/bge-m3-gguf/resolve/main/bge-m3-Q8_0.gguf"
        minMB = 300
    }
    "rerank" = @{
        file = "bge-reranker-v2-m3-q8_0.gguf"
        url = "https://huggingface.co/klnstpr/bge-reranker-v2-m3-Q8_0-GGUF/resolve/main/bge-reranker-v2-m3-q8_0.gguf"
        minMB = 300
    }
}

$wanted = @($Models -split "," | ForEach-Object { $_.Trim().ToLower() } | Where-Object { $_ })
$manifestChat = Join-Path $rt "models\chat\current.json"
New-Item -ItemType Directory -Force -Path (Join-Path $rt "models\chat") | Out-Null

foreach ($role in $wanted) {
    $p = $presets[$role]
    if (-not $p) { Write-Host "[--] Неизвестная роль модели: $role" -ForegroundColor DarkGray; continue }
    $dir = Join-Path $rt "models\$role"
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    $dest = Join-Path $dir $p.file
    $ok = (Test-Path $dest) -and ((Get-Item $dest).Length -gt ($p.minMB * 1MB))
    if ($ok -and -not $Force) {
        Write-Host "[ok] $role : $($p.file) уже на месте"
        continue
    }
    # Быстрый путь: модель уже скачана в старой установке LM Studio
    # (~/.lmstudio) — копируем, а не тянем повторно из сети. Ищем строго по
    # имени файла (на Windows сравнение регистронезависимое).
    $lmsDir = Join-Path $env:USERPROFILE ".lmstudio\models"
    if (Test-Path $lmsDir) {
        $old = Get-ChildItem -Path $lmsDir -Recurse -Filter $p.file -ErrorAction SilentlyContinue |
            Sort-Object Length -Descending | Select-Object -First 1
        if ($old -and $old.Length -gt ($p.minMB * 1MB)) {
            Write-Host "[..] Найдена модель из LM Studio: $($old.FullName) — копирую"
            Copy-Item $old.FullName $dest -Force
            Write-Host "[ok] Скопировано: $dest" -ForegroundColor Green
            continue
        }
    }
    Write-Host "[..] Скачиваю $role : $($p.file) (~$($p.minMB) МБ+)..."
    Write-Host "     $($p.url)"
    & curl.exe -L --fail --progress-bar -o "$dest.part" $p.url
    if ($LASTEXITCODE -eq 0 -and (Test-Path "$dest.part") -and
            ((Get-Item "$dest.part").Length -gt ($p.minMB * 1MB))) {
        Move-Item "$dest.part" $dest -Force
        Write-Host "[ok] Скачано: $dest" -ForegroundColor Green
    } else {
        Remove-Item "$dest.part" -Force -ErrorAction SilentlyContinue
        Write-Host "[!!] Не удалось скачать $($p.file). Скачайте вручную:" -ForegroundColor Yellow
        Write-Host "     $($p.url)  ->  $dest" -ForegroundColor Yellow
    }
}

# ---------- 4. Манифест активной чат-модели ----------
# current.json задаёт модель для спецификатора "shared:chat" во всех
# проектах: смена файла = смена модели у обоих сразу.
if ((($wanted -contains "chat") -or (Test-Path (Join-Path $rt "models\chat\*.gguf"))) -and -not (Test-Path $manifestChat)) {
    $chatFile = $presets["chat"].file
    if (-not (Test-Path (Join-Path $rt "models\chat\$chatFile"))) {
        $any = Get-ChildItem (Join-Path $rt "models\chat") -Filter *.gguf -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($any) { $chatFile = $any.Name }
    }
    $json = [pscustomobject]@{ file = $chatFile; switched_at = (Get-Date -Format s) }
    [IO.File]::WriteAllText($manifestChat, ($json | ConvertTo-Json), (New-Object System.Text.UTF8Encoding($false)))
    Write-Host "[ok] Активная чат-модель: $chatFile (models\chat\current.json)"
}

# ---------- 5. Регистрация проекта (для синхронной смены модели) ----------
if ($ProjectName -and $ProjectRoot) {
    $pj = Join-Path $rt "projects.json"
    $list = @()
    if (Test-Path $pj) {
        try { $list = @((Get-Content $pj -Raw -Encoding UTF8 | ConvertFrom-Json).projects) } catch { $list = @() }
    }
    $list = @($list | Where-Object { $_ -and $_.name -ne $ProjectName })
    $argsList = @()
    if ($RestartArgs) { $argsList = @($RestartArgs -split "\s+" | Where-Object { $_ }) }
    $list += [pscustomobject]@{ name = $ProjectName; root = (Resolve-Path $ProjectRoot).Path; restart_args = $argsList }
    [IO.File]::WriteAllText($pj, ([pscustomobject]@{ projects = $list } | ConvertTo-Json -Depth 6),
        (New-Object System.Text.UTF8Encoding($false)))
    Write-Host "[ok] Проект зарегистрирован в рантайме: $ProjectName ($ProjectRoot)"
}

Write-Host "[ok] Общий llama-рантайм готов: $rt"

