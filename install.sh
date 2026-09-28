#!/usr/bin/env bash
# ============================================================
#  Anonymizer Proxy — установщик macOS (M1–M4)
#  Один запуск: карантин → требования → окружение → зависимости →
#  общий llama-рантайм → .env → NER-модели → self-test → ярлыки.
#  Повторный запуск идемпотентен (ничего не перекачивает без нужды).
#
#  Аналог install.ps1 (Windows): те же шаги и тот же ОБЩИЙ
#  llama-рантайм машины (~/Library/Application Support/llama-runtime /
#  %LOCALAPPDATA%\llama-runtime), поэтому модели не дублируются
#  между проектами (anonymizer_proxy, hermes-disk-search и др.).
# ============================================================
set -euo pipefail
cd "$(dirname "$0")"

step() { printf '\n==> %s\n' "$1"; }
fail() { printf '[ОШИБКА] %s\n' "$1" >&2; exit 1; }
warn() { printf '[ПРЕДУПРЕЖДЕНИЕ] %s\n' "$1" >&2; }

# ---------- Аргументы ----------
# --skip-clients — не предлагать подключение Cline Desktop / Hermes Agent
SKIP_CLIENTS=0
for arg in "$@"; do
    case "$arg" in
        --skip-clients) SKIP_CLIENTS=1 ;;
        *) fail "Неизвестный аргумент: $arg (доступен --skip-clients)" ;;
    esac
done

# ---------- 0. Карантин Gatekeeper ----------
# Файлы, распакованные из скачанного браузером архива, получают метку
# com.apple.quarantine; без её снятия macOS блокирует неподписанные
# бинарники и .command-файлы («повреждён» / «не удаётся открыть»).
# Скрипт запущен из Терминала — снимать карантин здесь разрешено.
step "Снятие карантина Gatekeeper (com.apple.quarantine)"
if command -v xattr >/dev/null 2>&1; then
    xattr -dr com.apple.quarantine . 2>/dev/null || true
    echo "Метки карантина сняты (если были)."
else
    echo "xattr недоступен — пропускаю."
fi
# Бит исполнения: git-архивы не сохраняют права — восстанавливаем сами
chmod +x ./*.sh ./*.command 2>/dev/null || true

# ---------- 1. Системные требования ----------
# Пороги совпадают с install.ps1 (Windows) и таблицей README «Системные
# требования»: ОС macOS 12+, Apple Silicon, ОЗУ ≥ 8 ГБ, диск ≥ 10 ГБ.
# Переопределяются переменными окружения (для CI/автоматизации).
step "Проверка системных требований (Apple Silicon, macOS 12+, ОЗУ, место)"
OS_VER="$(sw_vers -productVersion 2>/dev/null || echo 0)"
OS_MAJOR="${OS_VER%%.*}"
OS_MAJOR="$(printf '%s' "$OS_MAJOR" | tr -cd '0-9')"
ARCH="$(uname -m)"
MIN_MACOS="${ANONYMIZER_MIN_MACOS:-12}"
MIN_RAM_GB="${ANONYMIZER_MIN_RAM_GB:-8}"
MIN_DISK_GB="${ANONYMIZER_MIN_DISK_GB:-10}"

if ! [ "${OS_MAJOR:-0}" -ge "$MIN_MACOS" ] 2>/dev/null; then
    fail "Требуется macOS ${MIN_MACOS}+ (обнаружена $OS_VER)."
fi
echo "ОС: macOS $OS_VER, $ARCH"

if [ "$ARCH" != "arm64" ]; then
    if [ "${ANONYMIZER_ALLOW_INTEL:-0}" = "1" ]; then
        warn "Требуется Apple Silicon — продолжаю по ANONYMIZER_ALLOW_INTEL=1 (без Metal-ускорения)."
    else
        fail "Требуется Apple Silicon (M1–M4); обнаружено $ARCH. Продолжить на свой риск: ANONYMIZER_ALLOW_INTEL=1 bash install.sh"
    fi
fi

RAM_BYTES="$(sysctl -n hw.memsize 2>/dev/null || echo 0)"
RAM_BYTES="$(printf '%s' "$RAM_BYTES" | tr -cd '0-9')"
RAM_GB=$(( ${RAM_BYTES:-0} / 1073741824 ))
if [ "$RAM_GB" -lt "$MIN_RAM_GB" ]; then
    fail "Недостаточно оперативной памяти: ${RAM_GB} ГБ (минимум ${MIN_RAM_GB} ГБ)."
elif [ "$RAM_GB" -lt 16 ]; then
    warn "ОЗУ ${RAM_GB} ГБ — работать будет, но рекомендуется 16 ГБ."
else
    echo "ОЗУ: ${RAM_GB} ГБ"
fi

FREE_KB="$(df -Pk . 2>/dev/null | awk 'NR==2 {print $4}')"
FREE_KB="$(printf '%s' "$FREE_KB" | tr -cd '0-9')"
FREE_GB=$(( ${FREE_KB:-0} / 1048576 ))
if [ "$FREE_GB" -lt "$MIN_DISK_GB" ]; then
    fail "Мало свободного места: ${FREE_GB} ГБ (минимум ${MIN_DISK_GB} ГБ; окружение + модели ~8 ГБ)."
elif [ "$FREE_GB" -lt 20 ]; then
    warn "Свободно ${FREE_GB} ГБ — рекомендуется 20 ГБ."
else
    echo "Свободное место: ${FREE_GB} ГБ"
fi

# ---------- 2. uv ----------
step "Проверка uv (менеджер окружения и Python)"
if ! command -v uv >/dev/null 2>&1; then
    echo "uv не найден — устанавливаю standalone-версию…"
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi
command -v uv >/dev/null 2>&1 || { echo "[ОШИБКА] uv не установился: https://docs.astral.sh/uv/"; exit 1; }
echo "uv: $(uv --version)"

# ---------- 3. Железо ----------
# На macOS официальный onnxruntime-wheel собран только с CPUExecutionProvider,
# поэтому ставим extra 'cpu'. Ускорение на M1–M4 даёт PyTorch-фоллбэк через
# MPS — прописываем NER_DEVICE=mps в .env ниже.
step "Аппаратная конфигурация: macOS → extra 'cpu' (+ NER_DEVICE=mps для torch-фоллбэка)"

# ---------- 4. Зависимости ----------
step "Установка зависимостей (uv sync --locked --extra cpu)"
uv sync --locked --extra cpu
PY=".venv/bin/python"

# ---------- 5. Общий llama-рантайм (llama.cpp + GGUF-модели) ----------
# Бинарь llama-server и модели — в ЕДИНОМ каталоге машины
# ($LLAMA_RUNTIME_DIR / ~/Library/Application Support/llama-runtime),
# общем с другими проектами (hermes-disk-search и т.п.): одна сборка
# llama.cpp (Metal), один набор моделей, синхронная смена чат-модели.
# Скрипт ставит llama.cpp из Homebrew (ссылка в bin/ рантайма) или,
# если brew нет, качает готовую сборку с GitHub Releases, снимая карантин.
# venv в PATH даёт скрипту python3 (нужен для GitHub API и регистрации
# проекта) даже на Mac без Command Line Tools — uv ставит Python сам.
step "Общий llama-рантайм машины (llama-server + GGUF-модели)"
if PATH="$(pwd)/.venv/bin:$PATH" bash scripts/ensure_llama_runtime.sh \
        --models chat \
        --project-name anonymizer_proxy \
        --project-root "$PWD" \
        --restart-args "-m anonymizer_proxy.llm_server restart"; then
    :
else
    warn "Общий llama-рантайм не готов: локальная модель не поднимется"
    warn "(облачные бэкенды и анонимизация работают как обычно)."
    warn "Повторите позже: bash scripts/ensure_llama_runtime.sh --models chat"
fi

# ---------- 6. Конфигурация .env ----------
step "Конфигурация .env"
if [ -f .env ]; then
    echo ".env уже существует — не трогаю (ваши настройки сохранены)."
else
    cp .env.example .env
    # PROXY_API_TOKEN — криптостойкий случайный токен
    TOKEN=$("$PY" -c "import secrets; print(secrets.token_urlsafe(32))")
    sed -i '' "s/PROXY_API_TOKEN=REPLACE_WITH_RANDOM_TOKEN/PROXY_API_TOKEN=$TOKEN/" .env
    # На M1–M4 torch-фоллбэк ускоряется через Metal (MPS)
    sed -i '' "s/NER_DEVICE=cpu/NER_DEVICE=mps/" .env
    echo "Создан .env, сгенерирован PROXY_API_TOKEN, NER_DEVICE=mps."
    while true; do
        printf 'Введите OPENROUTER_API_KEY (sk-or-v1-…), Enter — пропустить: '
        read -r KEY || KEY=""
        if [ -z "$KEY" ]; then
            echo "[ВНИМАНИЕ] Ключ не задан: анонимизация работать будет, а вот запросы"
            echo "к облаку упадут с 401. Впишите ключ с https://openrouter.ai/keys в .env."
            break
        fi
        case "$KEY" in
            sk-or-[A-Za-z0-9-]*)
                sed -i '' "s|OPENROUTER_API_KEY=sk-or-v1-REPLACE_WITH_YOUR_KEY|OPENROUTER_API_KEY=$KEY|" .env
                echo "Ключ OpenRouter записан."
                break
                ;;
            *)
                echo "Ключ должен начинаться с sk-or-. Проверьте вставку и попробуйте ещё раз."
                ;;
        esac
    done
    echo ""
    echo "Сразу после установки действует OpenRouter (нужен VPN)."
    echo "Действующего провайдера можно сменить в любой момент: российские"
    echo "GPTunneL, BotHub, AITUNNEL, GenAPI и свой endpoint работают без VPN —"
    echo "форма http://127.0.0.1:8081/env-editor (выбор провайдера и модели),"
    echo "POST /api/backend или переменная CLOUD_PROVIDER в .env."
fi

# ---------- 6a. Миграция .env: недостающие ключи из .env.example ----------
# Установщик НЕ перезаписывает существующий .env (настройки пользователя
# сохраняются), но при обновлении поверх СТАРОЙ установки схема конфига
# меняется. Идемпотентно добавляем отсутствующие ключи (например, секцию
# LLM_SERVER_* после перехода с LM Studio на llama-server) — без них прокси
# молча работает в устаревшей конфигурации. Заодно уводятся в комментарий
# устаревшие ключи и конфликтующий LOCAL_LLM_BASE_URL.
step "Миграция .env (недостающие ключи из .env.example)"
"$PY" scripts/ensure_env_keys.py || warn ".env не дополнен — проверьте LLM_SERVER_* вручную."

# ---------- 6b. GPU-аргументы llama-server под железо ----------
# llama.cpp по умолчанию считает всё на CPU; на Apple Silicon -ngl не нужен
# (Metal сам выгружает слои), а на машинах без GPU скрипт выставит -ngl 0.
# Если пользователь сам задал -ngl в .env — не перезаписываем.
step "Подбор GPU-аргументов llama-server (-ngl) под железо"
"$PY" scripts/configure_llm_args.py || echo "[ПРЕДУПРЕЖДЕНИЕ] Не удалось подобрать -ngl — .env оставлен без изменений."

# ---------- 7. Прогрев моделей ----------
step "Прогрев NER-моделей (GLiNER + Natasha, первый раз — скачивание)"
"$PY" scripts/download_models.py || echo "[ПРЕДУПРЕЖДЕНИЕ] Прогрев завершился с ошибками — см. выше."

# GGUF-модель локальной LLM уже обеспечена шагом 5 (общий llama-рантайм,
# models/chat общего каталога) — повторно ничего не скачивается.
# Своя модель мимо рантайма: .venv/bin/python scripts/download_llm_model.py
# (или LLM_SERVER_MODEL=<абсолютный путь к .gguf> в .env).

# ---------- 8. Самопроверка ----------
step "Самопроверка установки"
"$PY" scripts/install_selftest.py || echo "[ПРЕДУПРЕЖДЕНИЕ] Self-test FAIL — см. выше."

# ---------- 8b. Подключение клиентов (если установлены) ----------
# Автонастройка провайдера в установленных клиентах: standalone-приложение
# Cline Desktop (~/.cline) и Hermes Agent (config.yaml). Каждый вопрос
# пропускается ответом «n»; целиком отключить шаг — --skip-clients.
# Скрипты идемпотентны, делают резервные копии и требуют закрытого клиента
# (запущенный клиент перезаписывает свои настройки при выходе).
if [ "$SKIP_CLIENTS" != "1" ]; then
    step "Подключение клиентов (Cline Desktop / Hermes Agent), если установлены"
    if [ -d "$HOME/.cline" ]; then
        printf 'Найден Cline Desktop — подключить к прокси? [Y/n]: '
        read -r ANS || ANS=""
        case "${ANS:-}" in
            n|N|н|Н)
                echo "Пропущено. Позже: .venv/bin/python scripts/configure_cline.py --set-active" ;;
            *)
                if ! "$PY" scripts/configure_cline.py --set-active; then
                    warn "Не удалось — выполните позже:"
                    warn "  .venv/bin/python scripts/configure_cline.py --set-active"
                fi ;;
        esac
    else
        echo "Cline Desktop не найден (~/.cline отсутствует) — пропускаю."
    fi
    HERMES_CFG=""
    for c in "$HOME/.hermes/config.yaml" \
             "$HOME/Library/Application Support/hermes/config.yaml"; do
        if [ -f "$c" ]; then HERMES_CFG="$c"; break; fi
    done
    if [ -n "$HERMES_CFG" ]; then
        printf 'Найден Hermes Agent (%s) — подключить к прокси? [Y/n]: ' "$HERMES_CFG"
        read -r ANS || ANS=""
        case "${ANS:-}" in
            n|N|н|Н)
                echo "Пропущено. Позже: .venv/bin/python scripts/configure_hermes.py" ;;
            *)
                if ! "$PY" scripts/configure_hermes.py --file "$HERMES_CFG"; then
                    warn "Не удалось — выполните позже:"
                    warn "  .venv/bin/python scripts/configure_hermes.py --file \"$HERMES_CFG\""
                fi ;;
        esac
    else
        echo "Hermes Agent не найден (config.yaml отсутствует) — пропускаю."
    fi
fi

# ---------- 9. Ярлыки запуска ----------
step "Ярлыки запуска (двойной клик / Dock)"
chmod +x start_proxy.sh start_proxy.command 2>/dev/null || true
if bash make_mac_app.sh; then
    echo "  Anonymizer Proxy.app — перетащите в «Программы» или в Dock."
else
    echo "[ПРЕДУПРЕЖДЕНИЕ] Не удалось создать .app — запускайте через start_proxy.command."
fi
printf 'Включить автозапуск прокси при логине (LaunchAgent)? [y/N]: '
read -r AUTO_START || AUTO_START=""
case "$AUTO_START" in
    y|Y) bash install_launchagent.sh || echo "[ПРЕДУПРЕЖДЕНИЕ] LaunchAgent не установлен." ;;
    *)   echo "Автозапуск не включён. Включить позже: bash install_launchagent.sh" ;;
esac
echo "Держать локальную модель в памяти всегда (LaunchAgent llama-server,"
echo "независимо от прокси): bash install_launchagent.sh llama  (или all)."

# ---------- Финал ----------
echo ""
echo "============================================================"
echo " Установка завершена!"
echo "============================================================"
echo "Запуск:        ./start_proxy.sh  или двойной клик по start_proxy.command"
echo "Ярлык:         Anonymizer Proxy.app (можно перетащить в Dock)"
echo "Проверка:      http://127.0.0.1:8081/health"
echo ""
echo "Локальная модель (llama-server) — общий llama-рантайм машины:"
echo "  каталог:       ~/Library/Application Support/llama-runtime"
echo "                 (общий с hermes-disk-search; LLAMA_RUNTIME_DIR переопределяет)"
echo "  обзор:         .venv/bin/python -m anonymizer_proxy.llama_runtime list"
echo "  смена модели:  виджет в http://127.0.0.1:8081/env-editor или"
echo "                 .venv/bin/python -m anonymizer_proxy.llama_runtime switch <файл>"
echo "  диагностика:   .venv/bin/python -m anonymizer_proxy.llm_server status"
echo ""
echo "Подключение Cline (расширение VS Code):"
echo "  Base URL:  http://127.0.0.1:8081/v1"
echo "  API Key:   любое значение (прокси его игнорирует)"
echo "  Model:     любое значение"
echo ""
echo "Cline Desktop / Hermes Agent (если установлены) — скрипты подключения:"
echo "  .venv/bin/python scripts/configure_cline.py --set-active"
echo "  .venv/bin/python scripts/configure_hermes.py"
echo ""
echo "Правила анонимизации берутся из папки проекта:"
echo "  .clinerules уже в корне прокси (для Cline в этой папке)."
echo "  Для другого проекта с документами выполните:"
echo "    .venv/bin/python scripts/install_rules.py <путь-к-проекту> --clinerules"
echo "  (создаст AGENTS.md для Hermes и .clinerules для Cline)."
